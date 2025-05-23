# @ hwang258@jh.edu

import argparse
import os

# os.environ["CUDA_VISIBLE_DEVICES"]="0"
os.environ["USER"] = "zxy"  # TODO change this to your username

import shutil
import torch
import torchaudio
import numpy as np
import random
from argparse import Namespace
from data.tokenizer import (
    AudioTokenizer,
    TextTokenizer,
)
import torchaudio
import torchaudio.transforms as transforms
from edit_utils_zh import parse_edit_zh
from edit_utils_en import parse_edit_en
from inference_scale import inference_one_sample
import time
from tqdm import tqdm

# import argparse
from models import ssr
import re
from num2words import num2words
import uuid
import opencc
import nltk

# nltk.download("punkt")


def seed_everything(seed):
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"using {device}")


def replace_numbers_with_words(sentence):
    sentence = re.sub(r"(\d+)", r" \1 ", sentence)  # add spaces around numbers

    def replace_with_words(match):
        num = match.group(0)
        try:
            return num2words(num)  # Convert numbers to words
        except:
            return num  # In case num2words fails (unlikely with digits but just to be safe)

    return re.sub(
        r"\b\d+\b", replace_with_words, sentence
    )  # Regular expression that matches numbers


class WhisperxAlignModel:
    def __init__(self, language):
        from whisperx import load_align_model

        self.model, self.metadata = load_align_model(
            language_code=language, device=device
        )

    def align(self, segments, audio_path):
        from whisperx import align, load_audio

        audio = load_audio(audio_path)
        return align(
            segments,
            self.model,
            self.metadata,
            audio,
            device,
            return_char_alignments=False,
        )["segments"]


class WhisperModel:
    def __init__(self, model_name, language):
        from whisper import load_model

        self.model = load_model(model_name, device, language=language)

        from whisper.tokenizer import get_tokenizer

        tokenizer = get_tokenizer(multilingual=False, language=language)
        self.supress_tokens = [-1] + [
            i
            for i in range(tokenizer.eot)
            if all(c in "0123456789" for c in tokenizer.decode([i]).removeprefix(" "))
        ]

    def transcribe(self, audio_path):
        return self.model.transcribe(
            audio_path, suppress_tokens=self.supress_tokens, word_timestamps=True
        )["segments"]


class WhisperxModel:
    def __init__(self, model_name, align_model, language):
        from whisperx import load_model

        self.model = load_model(
            model_name,
            device,
            asr_options={
                "suppress_numerals": True,
                "max_new_tokens": None,
                "clip_timestamps": None,
                "hallucination_silence_threshold": None,
            },
            language=language,
            # vad_model_fp="/storage/zhangxueyao/workspace/SSR-Speech/pytorch_model.bin",
        )
        self.align_model = align_model

    def transcribe(self, audio_path):
        segments = self.model.transcribe(audio_path, batch_size=8)["segments"]
        for segment in segments:
            segment["text"] = replace_numbers_with_words(segment["text"])
        return self.align_model.align(segments, audio_path)


def get_transcribe_state(segments):
    words_info = [word_info for segment in segments for word_info in segment["words"]]
    transcript = " ".join([segment["text"] for segment in segments])
    transcript = transcript[1:] if transcript[0] == " " else transcript
    return {
        "segments": segments,
        "transcript": transcript,
    }


def transcribe(audio_path, transcribe_model):
    segments = transcribe_model.transcribe(audio_path)
    state = get_transcribe_state(segments)
    return state["transcript"], state["segments"]


def get_random_string():
    return "".join(str(uuid.uuid4()).split("-"))


def traditional_to_simplified(segments):
    converter = opencc.OpenCC("t2s")
    seg_num = len(segments)
    for i in range(seg_num):
        words = segments[i]["words"]
        for j in range(len(words)):
            segments[i]["words"][j]["word"] = converter.convert(
                segments[i]["words"][j]["word"]
            )
        segments[i]["text"] = converter.convert(segments[i]["text"])
    return segments


def align(args, segments, audio_path, align_model):
    segments = align_model.align(segments, audio_path)
    state = get_transcribe_state(segments)

    return state


def get_mask_interval(transcribe_state, word_span):
    seg_num = len(transcribe_state["segments"])
    data = []
    for i in range(seg_num):
        words = transcribe_state["segments"][i]["words"]
        for item in words:
            data.append([item["start"], item["end"], item["word"]])

    s, e = word_span[0], word_span[1]
    assert s <= e, f"s:{s}, e:{e}"
    assert s >= 0, f"s:{s}"
    assert e <= len(data), f"e:{e}"
    if e == 0:  # start
        start = 0.0
        end = float(data[0][0])
    elif s == len(data):  # end
        start = float(data[-1][1])
        end = float(data[-1][1])  # don't know the end yet
    elif s == e:  # insert
        start = float(data[s - 1][1])
        end = float(data[s][0])
    else:
        start = float(data[s - 1][1]) if s > 0 else float(data[s][0])
        end = float(data[e][0]) if e < len(data) else float(data[-1][1])

    return (start, end)


def parse_args():
    parser = argparse.ArgumentParser(description="inference speech editing")
    parser.add_argument(
        "--sub_amount",
        type=float,
        default=0.12,
        help="if the performance is not good, try modify this span, not used for tts",
    )
    parser.add_argument("--codec_audio_sr", type=int, default=16000)
    parser.add_argument("--codec_sr", type=int, default=50)
    parser.add_argument("--top_k", type=int, default=0)
    parser.add_argument("--top_p", type=float, default=0.8)
    parser.add_argument("--temperature", type=int, default=1)
    parser.add_argument("--kvcache", type=int, default=1)
    parser.add_argument("--seed", type=int, default=2024)
    parser.add_argument(
        "--stop_repetition",
        type=int,
        default=2,
        help="-1 means do not adjust prob of silence tokens. if there are long silence or unnaturally strecthed words, increase sample_batch_size to 2, 3 or even 4.",
    )
    parser.add_argument(
        "--sample_batch_size",
        type=int,
        default=1,
        help="what this will do to the model is that the model will run sample_batch_size examples of the same audio",
    )
    parser.add_argument("--cfg_coef", type=float, default=1.5)
    parser.add_argument("--cfg_stride", type=int, default=1)
    parser.add_argument("--aug_text", action="store_true", default=True)
    parser.add_argument("--aug_context", action="store_true", default=False)
    parser.add_argument("--use_watermark", action="store_true", default=True)
    parser.add_argument("--tts", action="store_true", default=False)
    parser.add_argument(
        "--prompt_length",
        type=int,
        default=3,
        help="used for tts prompt, will automatically cut the prompt audio to this length",
    )
    parser.add_argument(
        "--language",
        type=str,
        choices=["en", "zh"],
        default="zh",
        help="choose from en or zh",
    )
    parser.add_argument(
        "--model_path", type=str, default="./pretrained_models_mandarin/Mandarin.pth"
    )
    parser.add_argument(
        "--codec_path", type=str, default="./pretrained_models_mandarin/wmencodec.th"
    )
    parser.add_argument("--orig_audio", type=str, default="./demo/aishell3_test.wav")
    parser.add_argument(
        "--orig_transcript",
        type=str,
        default=None,
        help="not use now, a whisperx model will automatically do this",
    )
    parser.add_argument(
        "--target_transcript", type=str, default="食品价格以基本都在一万到两万之间"
    )
    parser.add_argument("--temp_folder", type=str, default="./demo/temp")
    parser.add_argument("--output_dir", type=str, default="./demo/generated_se")
    parser.add_argument("--savename", type=str, default="aishell3_test")
    parser.add_argument(
        "--whisper_model_name", type=str, choices=["base.en", "base"], default="base"
    )

    return parser.parse_args()


def main(args):
    seed_everything(args.seed)
    if args.language != "en" and args.language != "zh":
        raise RuntimeError("We only support English or Mandarin now!")

    # Initialize models
    filepath = os.path.join(args.model_path)
    ckpt = torch.load(filepath, map_location="cpu")
    model = ssr.SSR_Speech(ckpt["config"])
    model.load_state_dict(ckpt["model"])
    config = vars(model.args)
    phn2num = ckpt["phn2num"]
    model.to(device)
    model.eval()
    audio_tokenizer = AudioTokenizer(signature=args.codec_path)
    text_tokenizer = (
        TextTokenizer(backend="espeak")
        if args.language == "en"
        else TextTokenizer(backend="espeak", language="cmn")
    )  # use 'cmn-latn-pinyin' if your espeak-ng version is 1.51

    start_time = time.time()
    # move the audio and transcript to temp folder
    os.makedirs(args.temp_folder, exist_ok=True)
    os.makedirs(args.output_dir, exist_ok=True)
    os.system(f"cp {args.orig_audio} {args.temp_folder}")
    filename = os.path.splitext(args.orig_audio.split("/")[-1])[0]
    audio_fn = f"{args.temp_folder}/{filename}.wav"

    # resampling audio to 16k Hz
    import librosa
    import soundfile as sf

    audio, _ = librosa.load(audio_fn, sr=16000)
    sf.write(audio_fn, audio, 16000)

    print("*" * 5, "Loading WhisperxAlignModel", "*" * 5)
    print("args.language: ", args.language)
    align_model = WhisperxAlignModel(args.language)
    print("*" * 5, "Loading WhisperxModel", "*" * 5)
    print("args.whisper_model_name: ", args.whisper_model_name)
    transcribe_model = WhisperxModel(
        args.whisper_model_name, align_model, args.language
    )
    orig_transcript, segments = transcribe(audio_fn, transcribe_model)
    # if args.orig_transcript is None else args.orig_transcript
    if args.language == "zh":
        converter = opencc.OpenCC("t2s")
        orig_transcript = converter.convert(orig_transcript)
        target_transcript = args.target_transcript
        transcribe_state = align(
            args, traditional_to_simplified(segments), audio_fn, align_model
        )
        transcribe_state["segments"] = traditional_to_simplified(
            transcribe_state["segments"]
        )
    elif args.language == "en":
        orig_transcript = orig_transcript.lower()
        target_transcript = args.target_transcript.lower()
        transcribe_state = align(args, segments, audio_fn, align_model)
    print(orig_transcript)
    print(target_transcript)

    if args.tts:
        info = torchaudio.info(audio_fn)
        duration = info.num_frames / info.sample_rate
        cut_length = duration
        # Cut long audio for tts
        if duration > args.prompt_length:
            seg_num = len(transcribe_state["segments"])
            for i in range(seg_num):
                words = transcribe_state["segments"][i]["words"]
                for item in words:
                    if item["end"] >= args.prompt_length:
                        cut_length = min(item["end"], cut_length)

        audio, _ = librosa.load(audio_fn, sr=16000, duration=cut_length)
        sf.write(audio_fn, audio, 16000)
        orig_transcript, segments = transcribe(audio_fn, transcribe_model)

        if args.language == "zh":
            converter = opencc.OpenCC("t2s")
            orig_transcript = converter.convert(orig_transcript)
            transcribe_state = align(
                args, traditional_to_simplified(segments), audio_fn, align_model
            )
            transcribe_state["segments"] = traditional_to_simplified(
                transcribe_state["segments"]
            )
        elif args.language == "en":
            orig_transcript = orig_transcript.lower()
            target_transcript = args.target_transcript.lower()
            transcribe_state = align(args, segments, audio_fn, align_model)
        print(orig_transcript)
        target_transcript_copy = target_transcript  # for tts cut out
        if args.language == "en":
            target_transcript_copy = target_transcript_copy.split(" ")[0]
        elif args.language == "zh":
            target_transcript_copy = target_transcript_copy[0]
        target_transcript = (
            orig_transcript + " " + target_transcript
            if args.language == "en"
            else orig_transcript + target_transcript
        )
        print(target_transcript)

    # run the script to turn user input to the format that the model can take
    if not args.tts:
        operations, orig_spans = (
            parse_edit_en(orig_transcript, target_transcript)
            if args.language == "en"
            else parse_edit_zh(orig_transcript, target_transcript)
        )
        print(operations)
        print("orig_spans: ", orig_spans)

        if len(orig_spans) > 3:
            raise RuntimeError("Current model only supports maximum 3 editings")

        starting_intervals = []
        ending_intervals = []
        for orig_span in orig_spans:
            start, end = get_mask_interval(transcribe_state, orig_span)
            starting_intervals.append(start)
            ending_intervals.append(end)

        print("intervals: ", starting_intervals, ending_intervals)

        info = torchaudio.info(audio_fn)
        audio_dur = info.num_frames / info.sample_rate

        def combine_spans(spans, threshold=0.2):
            spans.sort(key=lambda x: x[0])
            combined_spans = []
            current_span = spans[0]

            for i in range(1, len(spans)):
                next_span = spans[i]
                if current_span[1] >= next_span[0] - threshold:
                    current_span[1] = max(current_span[1], next_span[1])
                else:
                    combined_spans.append(current_span)
                    current_span = next_span
            combined_spans.append(current_span)
            return combined_spans

        morphed_span = [
            [max(start - args.sub_amount, 0), min(end + args.sub_amount, audio_dur)]
            for start, end in zip(starting_intervals, ending_intervals)
        ]  # in seconds
        morphed_span = combine_spans(morphed_span, threshold=0.2)
        print("morphed_spans: ", morphed_span)
        save_morphed_span = f"{args.output_dir}/{args.savename}_mask.pt"
        torch.save(morphed_span, save_morphed_span)
        mask_interval = [
            [round(span[0] * args.codec_sr), round(span[1] * args.codec_sr)]
            for span in morphed_span
        ]
        mask_interval = torch.LongTensor(mask_interval)  # [M,2], M==1 for now
    else:
        info = torchaudio.info(audio_fn)
        audio_dur = info.num_frames / info.sample_rate

        morphed_span = [(audio_dur, audio_dur)]  # in seconds
        mask_interval = [
            [round(span[0] * args.codec_sr), round(span[1] * args.codec_sr)]
            for span in morphed_span
        ]
        mask_interval = torch.LongTensor(mask_interval)  # [M,2], M==1 for now
        print("mask_interval: ", mask_interval)

    decode_config = {
        "top_k": args.top_k,
        "top_p": args.top_p,
        "temperature": args.temperature,
        "stop_repetition": args.stop_repetition,
        "kvcache": args.kvcache,
        "codec_audio_sr": args.codec_audio_sr,
        "codec_sr": args.codec_sr,
    }

    for num in tqdm(range(args.sample_batch_size)):
        seed_everything(args.seed + num)
        new_audio = inference_one_sample(
            model,
            Namespace(**config),
            phn2num,
            text_tokenizer,
            audio_tokenizer,
            audio_fn,
            orig_transcript,
            target_transcript,
            mask_interval,
            args.cfg_coef,
            args.cfg_stride,
            args.aug_text,
            args.aug_context,
            args.use_watermark,
            args.tts,
            device,
            decode_config,
        )
        # save segments for comparison
        new_audio = new_audio[0].cpu()
        save_fn_new = f"{args.output_dir}/{args.savename}_new_seed{args.seed+num}.wav"
        torchaudio.save(save_fn_new, new_audio, args.codec_audio_sr)
        if args.tts:  # remove the start parts
            new_transcript, new_segments = transcribe(save_fn_new, transcribe_model)
            if args.language == "zh":
                transcribe_state = align(
                    args,
                    traditional_to_simplified(new_segments),
                    save_fn_new,
                    align_model,
                )
                transcribe_state["segments"] = traditional_to_simplified(
                    transcribe_state["segments"]
                )
                tmp1 = transcribe_state["segments"][0]["words"][0]["word"]
                tmp2 = target_transcript_copy
            elif args.language == "en":
                transcribe_state = align(args, new_segments, save_fn_new, align_model)
                tmp1 = transcribe_state["segments"][0]["words"][0]["word"].lower()
                tmp2 = target_transcript_copy.lower()
            if tmp1 == tmp2:
                offset = transcribe_state["segments"][0]["words"][0]["start"]
            else:
                offset = transcribe_state["segments"][0]["words"][1]["start"]
            audio, _ = librosa.load(save_fn_new, sr=16000, offset=offset)
            sf.write(save_fn_new, audio, 16000)

    save_fn_orig = f"{args.output_dir}/{args.savename}_orig.wav"
    shutil.copyfile(audio_fn, save_fn_orig)

    end_time = time.time()
    elapsed_time = end_time - start_time

    print(f"Running time: {elapsed_time:.4f} s")


if __name__ == "__main__":
    args = parse_args()
    main(args)
