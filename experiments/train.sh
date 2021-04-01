export CUDA_DEVICE_ORDER=PCI_BUS_ID
export CUDA_VISIBLE_DEVICES=0

cd src
# train
python main.py \
    ddd \
    --exp_id centerfusion \
    --shuffle_train \
    --train_split mini_train \
    --val_split mini_val \
    --val_intervals 1 \
    --run_dataset_eval \
    --nuscenes_att \
    --velocity \
    --eval \
    --batch_size 2 \
    --lr 2.5e-4 \
    --depth_adjustment_type exp \
    --factor 4 \
    --num_epochs 5 \
    --lr_step 50 \
    --save_point 20,40,50 \
    --gpus 0 \
    --not_rand_crop \
    --flip 0.5 \
    --shift 0.1 \
    --pointcloud \
    --radar_sweeps 3 \
    --pc_z_offset 0.0 \
    --pillar_dims 1.0,0.2,0.2 \
    --max_pc_dist 60.0 \
    --load_model ../models/centernet_baseline_e170.pth \
    # --freeze_backbone \
    # --resume \

cd ..
