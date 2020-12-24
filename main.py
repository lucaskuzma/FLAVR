import os
import sys
import time
import copy
import shutil
import random
import signal
import socket
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

import torch
import numpy as np
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter

import config
import myutils
from loss import Loss

from torch.utils.data import DataLoader

os.environ["CUDA_VISIBLE_DEVICES"] = str(7)

def load_checkpoint(args, model, optimizer , path):
    print("loading checkpoint %s" % path)
    checkpoint = torch.load(path)
    args.start_epoch = checkpoint['epoch'] + 1
    model.load_state_dict(checkpoint['state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer'])
    lr = checkpoint.get("lr" , args.lr)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


##### Parse CmdLine Arguments #####
args, unparsed = config.get_args()
cwd = os.getcwd()
print(args)

save_loc = os.path.join(args.checkpoint_dir , "saved_models_final" , args.dataset , args.exp_name)
if not os.path.exists(save_loc):
    os.makedirs(save_loc)
opts_file = os.path.join(save_loc , "opts.txt")
with open(opts_file , "w") as fh:
    fh.write(str(args))


##### TensorBoard & Misc Setup #####
if args.mode != 'test':
    writer_loc = os.path.join(args.checkpoint_dir , 'tensorboard_logs_%s_final/%s' % (args.dataset , args.exp_name))
    writer = SummaryWriter(writer_loc)

device = torch.device('cuda' if args.cuda else 'cpu')
torch.backends.cudnn.enabled = True
torch.backends.cudnn.benchmark = True

torch.manual_seed(args.random_seed)
if args.cuda:
    torch.cuda.manual_seed(args.random_seed)


if not args.step_between_clips:
   args.step_between_clips =  args.frames_per_clip*2

if args.dataset == "vimeo90K_septuplet":
    from dataset.vimeo90k_septuplet import get_loader
    train_loader = get_loader('train', args.data_root, args.batch_size, shuffle=True, num_workers=args.num_workers)
    test_loader = get_loader('test', args.data_root, args.test_batch_size, shuffle=False, num_workers=args.num_workers)   
elif args.dataset == "gopro":
    from dataset.GoPro import get_loader
    train_loader = get_loader(args.data_root, args.batch_size, shuffle=True, num_workers=args.num_workers, test_mode=False, interFrames=args.n_outputs, n_inputs=args.nbr_frame)
    test_loader = get_loader(args.data_root, args.batch_size, shuffle=False, num_workers=args.num_workers, test_mode=True, interFrames=args.n_outputs, n_inputs=args.nbr_frame)
else:
    raise NotImplementedError


from model.Unet_3D_3D_interpolate import UNet_3D_3D
print("Building model: %s"%args.model.lower())
model = UNet_3D_3D(args.model.lower() , n_inputs=args.nbr_frame, n_outputs=args.n_outputs, joinType=args.joinType, upmode=args.upmode)

# Just make every model to DataParallel
model = torch.nn.DataParallel(model).to(device)

##### Define Loss & Optimizer #####
criterion = Loss(args)

## ToDo: Different learning rate schemes for different parameters
from torch.optim import Adam
optimizer = Adam(model.parameters(), lr=args.lr, betas=(args.beta1, args.beta2))
print('# of parameters: %d' % sum(p.numel() for p in model.parameters()))

# Learning Rate Scheduler
scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
    optimizer, mode='min', factor=0.5, patience=5, verbose=True)

LOSS_0 = 0

def train(args, epoch):
    global LOSS_0
    losses, psnrs, ssims = myutils.init_meters(args.loss)
    model.train()
    criterion.train()

    t = time.time()
    for i, (images, gt_image) in enumerate(train_loader):

        # Build input batch
        images = [img_.cuda() for img_ in images]
        if args.n_outputs > 1:
            assert isinstance(gt_image , list) , "Required > 1 GT Frames for %sx interpolation ... "%(args.n_outputs+1)
            gt = [gt_.cuda() for gt_ in gt_image]
        else:
            gt = gt_image.cuda()

        # Forward
        optimizer.zero_grad()
        out = model(images)

        if args.n_outputs > 1:
            out = torch.cat(out)
            gt = torch.cat(gt)

        loss, loss_specific = criterion(out, gt)
        
        # Save loss values
        for k, v in losses.items():
            if k != 'total':
                v.update(loss_specific[k].item())
        if LOSS_0 == 0:
            LOSS_0 = loss.data.item()
        losses['total'].update(loss.item())

        loss.backward()
        optimizer.step()

        # Calc metrics & print logs
        if i % args.log_iter == 0: 
            myutils.eval_metrics(out, gt, psnrs, ssims)

            print('Train Epoch: {} [{}/{}]\tLoss: {:.6f}\tPSNR: {:.4f}\tTime({:.2f})'.format(
                epoch, i, len(train_loader), losses['total'].avg, psnrs.avg, time.time() - t) , flush=True)
            
            # Log to TensorBoard
            timestep = epoch * len(train_loader) + i
            writer.add_scalar('Loss/train', loss.data.item(), timestep)
            writer.add_scalar('PSNR/train', psnrs.avg, timestep)
            writer.add_scalar('SSIM/train', ssims.avg, timestep)
            writer.add_scalar('lr', optimizer.param_groups[-1]['lr'], timestep)

            # Reset metrics
            losses, psnrs, ssims = myutils.init_meters(args.loss)
            t = time.time()


def test(args, epoch, eval_alpha=0.5):
    print('Evaluating for epoch = %d' % epoch)
    losses, psnrs, ssims = myutils.init_meters(args.loss)
    model.eval()
    criterion.eval()
        
    t = time.time()
    with torch.no_grad():
        for i, (images, gt_image) in enumerate(tqdm(test_loader)):

            images = [img_.cuda() for img_ in images]
            if args.n_outputs > 1:
                assert isinstance(gt_image , list)
                gt = [gt_.cuda() for gt_ in gt_image]
            else:
                gt = gt_image.cuda()
            out = model(images) ## images is a list of neighboring frames

            if args.n_outputs > 1:
                out = torch.cat(out)
                gt = torch.cat(gt)

            # Save loss values
            loss, loss_specific = criterion(out, gt)
            for k, v in losses.items():
                if k != 'total':
                    v.update(loss_specific[k].item())
            losses['total'].update(loss.item())

            # Evaluate metrics
            myutils.eval_metrics(out, gt, psnrs, ssims)
                    
    # Print progress
    print("Loss: %f, PSNR: %f, SSIM: %f\n" %
          (losses['total'].avg, psnrs.avg, ssims.avg))

    # Save psnr & ssim
    save_fn = os.path.join(save_loc, 'results.txt')
    with open(save_fn, 'a') as f:
        f.write('For epoch=%d\t' % epoch)
        f.write("PSNR: %f, SSIM: %f\n" %
                (psnrs.avg, ssims.avg))

    # Log to TensorBoard
    if args.mode != 'test':

        timestep = epoch +1
        writer.add_scalar('Loss/test', loss.data.item(), timestep)
        writer.add_scalar('PSNR/test', psnrs.avg, timestep)
        writer.add_scalar('SSIM/test', ssims.avg, timestep)

    return losses['total'].avg, psnrs.avg, ssims.avg


""" Entry Point """
def main(args):
    if args.mode == 'test':
        assert args.load_from is not None
        load_checkpoint(args, model, optimizer)
        _, _, _, _ = test(args, args.start_epoch)
        return

    best_psnr = 0
    for epoch in range(args.start_epoch, args.max_epoch):
        train(args, epoch)

        if epoch % 2 == 0:
            test_loss, psnr, _ = test(args, epoch)

        # save checkpoint
        is_best = psnr > best_psnr
        best_psnr = max(psnr, best_psnr)
        myutils.save_checkpoint({
            'epoch': epoch,
            'state_dict': model.state_dict(),
            'optimizer': optimizer.state_dict(),
            'best_psnr': best_psnr,
            'lr' : optimizer.param_groups[-1]['lr']
        }, save_loc, is_best, args.exp_name)

        # update optimizer policy
        scheduler.step(test_loss)

if __name__ == "__main__":
    main(args)