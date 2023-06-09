import argparse
import os
import random
import time
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data
import torchvision.datasets as dset
import torchvision.transforms as transforms
import torchvision.utils as vutils
import einops as ein

from FID.InceptionNet import model as inception_model
from FID.FID import calculate_fretchet

# Needed to download datasets on sites without ssl/https
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', required=True, help='cifar10 | lsun | mnist |imagenet | folder | lfw | fake')
    parser.add_argument('--dataroot', required=False, help='path to dataset')
    parser.add_argument('--workers', type=int, help='number of data loading workers', default=2)
    parser.add_argument('--batchSize', type=int, default=64, help='input batch size')
    parser.add_argument('--imageSize', type=int, default=64, help='the height / width of the input image to network')
    parser.add_argument('--ngf', type=int, default=64)
    parser.add_argument('--niter', type=int, default=25, help='number of epochs to train for')
    parser.add_argument('--lr', type=float, default=0.0002, help='learning rate, default=0.0002')
    parser.add_argument('--beta1', type=float, default=0.5, help='beta1 for adam. default=0.5')
    parser.add_argument('--cuda', action='store_true', help='enables cuda')
    parser.add_argument('--dry-run', action='store_true', help='check a single training cycle works')
    parser.add_argument('--ngpu', type=int, default=1, help='number of GPUs to use')
    parser.add_argument('--netG', default='', help="path to netG (to continue training)")
    parser.add_argument('--outf', default='.', help='folder to output images and model checkpoints')
    parser.add_argument('--manualSeed', type=int, help='manual seed')
    parser.add_argument('--classes', default='bedroom', help='comma separated list of classes for the lsun data set')

    opt = parser.parse_args()
    print(opt)

    try:
        os.makedirs(opt.outf)
    except OSError:
        pass

    if opt.manualSeed is None:
        opt.manualSeed = random.randint(1, 10000)
    print("Random Seed: ", opt.manualSeed)
    random.seed(opt.manualSeed)
    torch.manual_seed(opt.manualSeed)

    cudnn.benchmark = True

    if torch.cuda.is_available() and not opt.cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")
    
    if opt.dataroot is None and str(opt.dataset).lower() != 'fake':
        raise ValueError("`dataroot` parameter is required for dataset \"%s\"" % opt.dataset)

    if opt.dataset in ['imagenet', 'folder', 'lfw']:
        # folder dataset
        dataset = dset.ImageFolder(root=opt.dataroot,
                                transform=transforms.Compose([
                                    transforms.Resize(opt.imageSize),
                                    transforms.CenterCrop(opt.imageSize),
                                    transforms.ToTensor(),
                                    transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                                ]))
        nc=3
    elif opt.dataset == 'lsun':
        classes = [ c + '_train' for c in opt.classes.split(',')]
        dataset = dset.LSUN(root=opt.dataroot, classes=classes,
                            transform=transforms.Compose([
                                transforms.Resize(opt.imageSize),
                                transforms.CenterCrop(opt.imageSize),
                                transforms.ToTensor(),
                                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                            ]))
        nc=3
    elif opt.dataset == 'cifar10':
        dataset = dset.CIFAR10(root=opt.dataroot, download=True,
                            transform=transforms.Compose([
                                transforms.Resize(opt.imageSize),
                                transforms.ToTensor(),
                                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
                            ]))
        nc=3

    elif opt.dataset == 'mnist':
            dataset = dset.MNIST(root=opt.dataroot, download=True,
                            transform=transforms.Compose([
                                transforms.Resize(opt.imageSize),
                                transforms.RandomHorizontalFlip(),
                                transforms.RandomRotation(30, interpolation=transforms.InterpolationMode.BILINEAR),
                                transforms.ToTensor(),
                                transforms.Normalize((0.5,), (0.5,)),
                            ]))
            nc=1

    elif opt.dataset == 'fake':
        dataset = dset.FakeData(image_size=(3, opt.imageSize, opt.imageSize),
                                transform=transforms.ToTensor())
        nc=3

    assert dataset
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=opt.batchSize,
                                            shuffle=True, num_workers=int(opt.workers))

    device = torch.device("cuda:0" if opt.cuda else "cpu")
    ngpu = int(opt.ngpu)
    ngf = int(opt.ngf)
    z_res = 4

    # custom weights initialization called on netG and netD
    def weights_init(m):
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            torch.nn.init.normal_(m.weight, 0.0, 0.02)
        elif classname.find('BatchNorm') != -1:
            torch.nn.init.normal_(m.weight, 1.0, 0.02)
            torch.nn.init.zeros_(m.bias)


    class Generator(nn.Module):
        def __init__(self):
            super(Generator, self).__init__()
            self.main = nn.Sequential(
                # nn.Linear(z_res**2, z_res**2),
                # nn.Linear(z_res**2, z_res**2),

                nn.Unflatten(1, (z_res**2, 1, 1)),

                # input is Z, going into a convolution
                nn.ConvTranspose2d(     z_res**2, ngf * 8, 4, 1, 0, bias=False),
                nn.BatchNorm2d(ngf * 8),
                nn.ReLU(True),
                # state size. (ngf*8) x 4 x 4
                nn.ConvTranspose2d(ngf * 8, ngf * 4, 4, 2, 1, bias=False),
                nn.BatchNorm2d(ngf * 4),
                nn.ReLU(True),
                # state size. (ngf*4) x 8 x 8
                nn.ConvTranspose2d(ngf * 4, ngf * 2, 4, 2, 1, bias=False),
                nn.BatchNorm2d(ngf * 2),
                nn.ReLU(True),
                # state size. (ngf*2) x 16 x 16
                nn.ConvTranspose2d(ngf * 2,     ngf, 4, 2, 1, bias=False),
                nn.BatchNorm2d(ngf),
                nn.ReLU(True),
                # state size. (ngf) x 32 x 32
                nn.ConvTranspose2d(    ngf,      nc, 4, 2, 1, bias=False),
                nn.Tanh()
                # state size. (nc) x 64 x 64
            )

        def forward(self, input):
            return self.main(input)


    netG = Generator().to(device)
    netG.apply(weights_init)
    if opt.netG != '':
        netG.load_state_dict(torch.load(opt.netG))
    # print(netG)
    print("# of parameters in G:", sum(p.numel() for p in netG.parameters() if p.requires_grad))

    inception_model = inception_model.to(device)

    criterion = nn.L1Loss()

    fixed_noise = torch.randn(opt.batchSize, z_res**2, device=device)
    real_label = 1
    fake_label = 0

    # setup optimizer
    optimizerG = optim.Adam(netG.parameters(), lr=opt.lr, betas=(opt.beta1, 0.999))

    if opt.dry_run:
        opt.niter = 1

    r = torch.randperm(z_res**2)
    r2 = torch.randperm((z_res**2)**2)
    rand = torch.rand([z_res**2])

    for epoch in range(opt.niter):
        for i, data in enumerate(dataloader, 0):
            input = data[0].to(device)
            # input = ein.reduce(input, "b c (h i) (w j) -> b (h w)", "mean", i=opt.imageSize//z_res, j=opt.imageSize//z_res) # Avg pool to 8x8 then to z
            
            # # NOTE: Maybe shuffle here?
            # input = input[:,r]

            netG.zero_grad()
            # output = netG(input)

            with torch.no_grad():
                t = input.flatten(1)[:, r2]
                t = ein.reduce(t, "b (n i) -> b n", "mean", i=z_res**2)
                t = t.argsort(1)
                t = rand[t]

            output = netG(t)
            errG = criterion(output, input)
            errG.backward()
            optimizerG.step()

            if i % 100 == 0:
                print('[%d/%d][%d/%d] Loss_G: %.4f '
                    % (epoch, opt.niter, i, len(dataloader), errG.item()))
                vutils.save_image(data[0],
                        '%s/real_samples.png' % opt.outf,
                        normalize=True)
                with torch.no_grad():
                    fake = netG(fixed_noise)
                vutils.save_image(fake.detach(),
                        '%s/fake_samples_epoch_%03d_%03d.png' % (opt.outf, epoch, i),
                        normalize=True)

            if opt.dry_run:
                break
        # # Save FID
        # with torch.no_grad():
        #     if real_cpu.shape[1] == 1:
        #         real_cpu = real_cpu.expand(-1, 3, -1, -1)
        #         fake = fake.expand(-1, 3, -1, -1)
        #     fid = calculate_fretchet(real_cpu, fake, inception_model)
        # with open(f"{opt.outf}/fid.txt", "a+") as file:
        #     file.write(f"{epoch}, {i}, {round(time.time())}, {fid}\n")

        # do checkpointing
        torch.save(netG.state_dict(), '%s/netG_epoch_%d.pth' % (opt.outf, epoch))
