from randomwalker.RandomWalkerModule import RandomWalker
import torch
import numpy as np
from randomwalker.randomwalker_loss_utils import NHomogeneousBatchLoss
from unet.unet import UNet
from utils.evaluation_utils import compute_iou
import time
import os
import yaml
from argparse import Namespace

from tqdm import tqdm

from torchvision import transforms
from data.cremi_dataloader import CremiSegmentationDataset
from utils.seed_utils import sample_seeds
from utils.plotting_utils import save_summary_plot
from train import get_base_parser


def parse_args():
    parser = get_base_parser(description='Train on a single neuronal image')
    parser.add_argument('--image-index', dest='img_idx', type=int, default=0)
    parser.add_argument('--load', type=str, default=False, help='Load model from a .pth file')

    args = parser.parse_args()

    if args.config is not None:
        with open(args.config, "r") as stream:
            config_params = yaml.load(stream, Loader=yaml.FullLoader)
            args_dict = vars(args)
            args_dict.update(config_params)
            args = Namespace(**args_dict)

    return args

def run_transductive_experiment(args, raw, seeds, mask_x, mask_y, num_classes, summary_callback=None, model_path=False):
    # Init the UNet
    unet = UNet(1, args.unet_channels, args.unet_blocks)
    if model_path:
        unet.load_state_dict(torch.load(model_path))

    # Init the random walker modules
    rw = RandomWalker(args.sampled_gradients, max_backprop=args.gradient_pruning)

    # Init optimizer
    optimizer = torch.optim.AdamW(unet.parameters(), lr=0.01, weight_decay=args.weight_decay)

    # Loss has to been wrapped in order to work with random walker algorithm
    loss_fn = NHomogeneousBatchLoss(torch.nn.NLLLoss)

    # Main overfit loop
    total_time = 0.0
    num_it = 0
    for it in tqdm(range(iterations + 1)):
        t1 = time.time()
        optimizer.zero_grad()

        net_output = unet(raw.unsqueeze(0))

        # Diffusivities must be positive
        diffusivities = torch.sigmoid(net_output)

        # Random walker
        output = rw(diffusivities, seeds)

        # Loss and diffusivities update
        output_log = [torch.log(o)[:, :, mask_x, mask_y] for o in output]

        l = loss_fn(output_log, target[:, :, mask_x, mask_y])
        l.backward(retain_graph=True)
        optimizer.step()

        t2 = time.time()
        total_time += t2 - t1
        num_it += 1

        # Summary
        if it % 5 == 0 and summary_callback is not None:
            pred = torch.argmax(output[0], dim=1)
            iou_score = compute_iou(pred, target[0], num_classes)
            avg_time = total_time / num_it
            summary_callback(raw, seeds, mask, diffusivities, output, target, it, avg_time, l, iou_score, model_path)
            total_time = 0.0
            num_it = 0


def transductive_summary(raw, seeds, mask, diffusivities, output, target, it, avg_time, l, iou_score, model_path):
    print(f"Iteration {it}  Time/iteration(s): {avg_time}  Loss: {l.item()}  mIoU: {iou_score}",
          file=log_file)
    save_summary_plot(raw, target, seeds, diffusivities, output, mask, args.subsampling_ratio,
                      it, iou_score, save_path)


if __name__ == '__main__':
    args = parse_args()

    with open(args.config, "r") as stream:
        config_params = yaml.load(stream, Loader=yaml.FullLoader)
        args_dict = vars(args)
        args_dict.update(config_params)
        args = Namespace(**args_dict)

    save_path = f"transductive-image-{args.img_idx}-seeds-{args.seeds_per_region}"
    if not os.path.exists(save_path):
        os.makedirs(save_path)
    log_file = open(f"{save_path}/log.txt", "a+")
    print(save_path, file=log_file)

    # Init parameters
    batch_size = 1
    iterations = args.max_epochs
    size = (args.resolution, args.resolution)
    datadir = "data/"

    # Load data and init
    raw_transforms = transforms.Compose([
        transforms.Normalize(mean=[0.5], std=[0.5]),
        transforms.CenterCrop(size=size),
    ])
    target_transforms = transforms.Compose([
        transforms.CenterCrop(size=size),
    ])

    # loading in dataset
    train_dataset = CremiSegmentationDataset("data/sample_A_20160501.hdf", transform=raw_transforms,
                                             target_transform=target_transforms,
                                             subsampling_ratio=args.subsampling_ratio, split="train")
    raw, target, mask = train_dataset[args.img_idx]
    target = target.unsqueeze(0)
    num_classes = len(np.unique(target))

    # Define sparse mask for the loss
    mask = mask.squeeze()
    mask_x, mask_y = mask.nonzero(as_tuple=True)
    masked_targets = target.squeeze()[mask_x, mask_y]

    # Generate seeds
    seeds = sample_seeds(args.seeds_per_region, target, masked_targets, mask_x, mask_y, num_classes)

    # Run transductive experiment
    run_transductive_experiment(args, raw, seeds, mask_x, mask_y, num_classes, summary_callback=transductive_summary,
                                model_path=args.load)
