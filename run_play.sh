# Chinese
python inference_v2.py \
    --seed 2024 \
    --sub_amount 0.12 \
    --cfg_stride 1 \
    --aug_text \
    --use_watermark \
    --language 'zh' \
    --model_path "./pretrained_models_mandarin/Mandarin.pth" \
    --codec_path "./pretrained_models_mandarin/wmencodec.th" \
    --orig_audio "./demo/aishell3_test.wav" \
    --target_transcript "食品价格以基本都在一万到两万之间" \
    --temp_folder "./demo/temp" \
    --output_dir "./demo/generated_se" \
    --savename "aishell3_test" \
    --whisper_model_name "base"