from randomwalker.RandomWalkerModule import RandomWalker
import torch
from data.datapreprocessing.target_sparse_sampling import SparseMaskTransform
import matplotlib.pyplot as plt
import numpy as np
from randomwalker.randomwalker_loss_utils import NHomogeneousBatchLoss
from unet.unet import UNet
from utils.evaluation_utils import compute_iou
import time
import os
import argparse

from tqdm import tqdm

from torchvision import transforms
from data.cremiDataloading import CremiSegmentationDataset
from utils.seed_utils import sample_seeds
from utils.plotting_utils import save_summary_plot


def parse_args():
    parser = argparse.ArgumentParser(description='Train on a single neuronal image')
    parser.add_argument('--max-epochs', type=int, default=40, help='Maximum number of epochs')
    parser.add_argument('--batch-size', dest='batch_size', type=int, default=1, help='Batch size')
    parser.add_argument('--lr', dest='lr', type=float, default=1e-3, help='Learning rate')
    parser.add_argument('--weight-decay', dest='weight_decay', type=float, default=1e-2,
                        help='Weight decay')
    parser.add_argument('--image-index', dest='img_idx', type=int, default=0)
    parser.add_argument('--load', type=str, default=False, help='Load model from a .pth file')
    parser.add_argument('--diffusivity-threshold', dest="diffusivity_threshold", type=float, default=False,
                        help='Diffusivity threshold')
    parser.add_argument('--rw-num-grad', dest="rw_num_grad", type=int, default=1000,
                        help='Number of sampled gradients for Random Walker backprop')
    parser.add_argument('--rw-max-backprop', dest="rw_max_backprop", type=bool, default=True,
                        help='Whether to use gradient pruning in Random Walker backprop')
    parser.add_argument('--subsampling-ratio', dest="subsampling_ratio", type=float, default=0.5,
                        help='Subsampling ratio')
    parser.add_argument('--seeds-per-region', dest="seeds_per_region", type=int, default=5,
                        help='Seeds per Region')

    return parser.parse_args()


if __name__ == '__main__':
    args = parse_args()

    save_path = f"transductive-image-{args.img_idx}-seeds-{args.seeds_per_region}"
    if not os.path.exists(f"results/{save_path}"):
        os.makedirs(f"results/{save_path}")
    log_file = open(f"results/{save_path}/log.txt", "a+")
    print(save_path, file=log_file)

    # Init parameters
    batch_size = 1
    iterations = 50
    size = (128, 128)
    datadir = "data/"

    # Load data and init
    raw_transforms = transforms.Compose([
        transforms.Normalize(mean=[0.5], std=[0.5]),
        transforms.FiveCrop(size=size),
    ])
    target_transforms = transforms.Compose([
        transforms.FiveCrop(size=size),
    ])

    # loading in dataset
    train_dataset = CremiSegmentationDataset("data/sample_A_20160501.hdf", transform=raw_transforms,
                                             target_transform=target_transforms, subsampling_ratio=0.1,
                                             split="validation")
    raw, target, _ = train_dataset[args.img_idx]
    target = target.unsqueeze(0)

    num_classes = len(np.unique(target))

    # Define sparse mask for the loss
    mask = SparseMaskTransform(args.subsampling_ratio)(target.squeeze())
    mask_x, mask_y = mask.nonzero(as_tuple=True)
    masked_targets = target.squeeze()[mask_x, mask_y]

    # Generate seeds
    seeds = sample_seeds(args.seeds_per_region, target, masked_targets, mask_x, mask_y, num_classes)

    print(f"\n Gradient Pruned: {args.rw_max_backprop}, Subsampling ratio: {args.subsampling_ratio}", file=log_file)

    # Init the UNet
    unet = UNet(1, 32, 3)

    # Init the random walker modules
    rw = RandomWalker(args.rw_num_grad, max_backprop=args.rw_max_backprop)

    # Init optimizer
    optimizer = torch.optim.AdamW(unet.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    # Loss has to been wrapped in order to work with random walker algorithm
    loss = NHomogeneousBatchLoss(torch.nn.NLLLoss)

    # Main overfit loop
    total_time = 0.0
    num_it = 0
    for it in tqdm(range(iterations + 1)):
        t1 = time.time()
        optimizer.zero_grad()

        diffusivities = unet(raw.unsqueeze(0))

        # Diffusivities must be positive
        net_output = torch.sigmoid(diffusivities)

        # Random walker
        output = rw(net_output, seeds)

        # Loss and diffusivities update
        output_log = [torch.log(o + 1e-10)[:, :, mask_x, mask_y] for o in output]

        l = loss(output_log, target[:, :, mask_x, mask_y])

        l.backward(retain_graph=True)
        optimizer.step()

        t2 = time.time()
        total_time += t2-t1
        num_it += 1

        # Summary
        if it % 5 == 0:
            pred = torch.argmax(output[0], dim=1)
            iou_score = compute_iou(pred, target[0], num_classes)
            avg_time = total_time / num_it
            print(f"Iteration {it}  Time/iteration(s): {avg_time}  Loss: {l.item()}  mIoU: {iou_score}",
                  file=log_file)
            save_summary_plot(raw, target, seeds, diffusivities, output, mask, args.subsampling_ratio,
                              it, iou_score, save_path)
            total_time = 0.0
            num_it = 0
