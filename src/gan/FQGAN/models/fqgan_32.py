import torch
import torch.nn as nn
from apex import amp
from .fq import FeatureQuantizer, FeatureQuantizerEMA
from torch_mimicry.nets import gan
from torch_mimicry.modules import SNLinear
from torch_mimicry.modules import GBlock, DBlock, DBlockOptimized


class FQGANGenerator(gan.BaseGenerator):
    def __init__(self,
                 nz=128,
                 ngf=256,
                 bottom_width=4,
                 loss_type="hinge",
                 fq_strength=10.0,
                 is_amp=False,
                 **kwargs):
        super().__init__(nz=nz,
                         ngf=ngf,
                         bottom_width=bottom_width,
                         loss_type=loss_type,
                         **kwargs)

        self.fq_strength = fq_strength
        self.is_amp = is_amp

        self.l1 = nn.Linear(self.nz, (self.bottom_width**2) * self.ngf)
        self.block2 = GBlock(self.ngf, self.ngf, upsample=True)
        self.block3 = GBlock(self.ngf, self.ngf, upsample=True)
        self.block4 = GBlock(self.ngf, self.ngf, upsample=True)
        self.b5 = nn.BatchNorm2d(self.ngf)
        self.c5 = nn.Conv2d(self.ngf, 3, 3, 1, padding=1)
        self.activation = nn.ReLU(True)

        # Initialise the weights
        nn.init.xavier_uniform_(self.l1.weight.data, 1.0)
        nn.init.xavier_uniform_(self.c5.weight.data, 1.0)

    def forward(self, x):

        h = self.l1(x)
        h = h.view(x.shape[0], -1, self.bottom_width, self.bottom_width)
        h = self.block2(h)
        h = self.block3(h)
        h = self.block4(h)
        h = self.b5(h)
        h = self.activation(h)
        h = self.c5(h)

        return torch.tanh(h)

    def train_step(self,
                   real_batch,
                   netD,
                   optG,
                   log_data,
                   device=None,
                   global_step=None,
                   **kwargs):

        self.zero_grad()

        # Get only batch size from real batch
        batch_size = real_batch[0].shape[0]

        # Produce fake images and logits
        fake_images = self.generate_images(num_images=batch_size,
                                           device=device)
        output, quant_loss, _ = netD(fake_images)

        # Compute GAN loss, upright images only.
        errG = self.compute_gan_loss(output)

        # Backprop and update gradients
        errG_total = errG + self.fq_strength * quant_loss
        # backward using apex.amp
        if self.is_amp:
            with amp.scale_loss(errG_total, optG) as scaled_loss:
                scaled_loss.backward()
        else:
            errG_total.backward()

        optG.step()

        # Log statistics
        log_data.add_metric('errG', errG, group='loss')
        if quant_loss != 0:
            log_data.add_metric('errG_quant', quant_loss, group='loss_quant')

        return log_data


class FQGANDiscriminator(gan.BaseDiscriminator):
    def __init__(self,
                 ndf=128,
                 loss_type="hinge",
                 fq_type=None,
                 dict_size=10,
                 quant_layers=None,
                 fq_strength=10.0,
                 is_amp=False,
                 **kwargs):
        super().__init__(ndf=ndf,
                         loss_type=loss_type,
                         **kwargs)

        self.fq_type = fq_type
        self.dict_size = dict_size
        self.quant_layers = quant_layers
        self.fq_strength = fq_strength
        self.is_amp = is_amp

        if isinstance(self.quant_layers, int):
            self.quant_layers = [self.quant_layers]

        if self.fq_type is not None:
            assert self.fq_type in ['Normal', 'EMA'], "set fq_type within ['Normal', 'EMA']"

        # Build layers
        self.block1 = DBlockOptimized(3, self.ndf)
        self.block2 = DBlock(self.ndf, self.ndf, downsample=True)
        self.block3 = DBlock(self.ndf, self.ndf, downsample=False)
        self.block4 = DBlock(self.ndf, self.ndf, downsample=False)
        self.l5 = SNLinear(self.ndf, 1)
        self.activation = nn.ReLU(True)

        # Initialise the weights
        nn.init.xavier_uniform_(self.l5.weight.data, 1.0)

        if self.fq_type:
            assert self.quant_layers is not None, "should set quant_layers like ['3']"
            assert (min(self.quant_layers) > 1) and (max(self.quant_layers) < 5), "should be range [2, 4]"
            for layer in self.quant_layers:
                out_channels = getattr(self, f"block{layer}").out_channels
                if self.fq_type == "Normal":
                    setattr(self, f"fq{layer}", FeatureQuantizer(out_channels, 2 ** self.dict_size))
                elif self.fq_type == "EMA":
                    setattr(self, f"fq{layer}", FeatureQuantizerEMA(out_channels, 2 ** self.dict_size))

    def forward(self, x):

        h = x
        h = self.block1(h)

        # compute quant layer
        quant_loss = 0
        embed_idx = None
        for layer in range(2, 5):
            h = getattr(self, f"block{layer}")(h)
            # apply Feature Quantization
            if (self.fq_type) and (layer in self.quant_layers):
                h, loss, embed_idx = getattr(self, f"fq{layer}")(h)
                quant_loss += loss

        # Global sum pooling
        h = torch.sum(h, dim=(2, 3))
        output = self.l5(h)

        return output, quant_loss, embed_idx

    def train_step(self,
                   real_batch,
                   netG,
                   optD,
                   log_data,
                   device=None,
                   global_step=None,
                   **kwargs):

        self.zero_grad()

        # produce real images
        real_images, _ = real_batch
        batch_size = real_images.shape[0]

        # produce fake images
        fake_images = netG.generate_images(num_images=batch_size,
                                           device=device).detach()

        # comput real and fake logits for gan loss
        output_real, quant_loss_real, _ = self.forward(real_images)
        output_fake, quant_loss_fake, _ = self.forward(fake_images)

        # compute GAN loss.
        errD = self.compute_gan_loss(output_real=output_real,
                                     output_fake=output_fake)

        # backprop and update gradients
        errD_total = errD + self.fq_strength * (quant_loss_real + quant_loss_fake)
        if self.is_amp:
            with amp.scale_loss(errD_total, optD) as scaled_loss:
                scaled_loss.backward()
        else:
            errD_total.backward()

        optD.step()

        # Compute probabilities
        D_x, D_Gz = self.compute_probs(output_real=output_real,
                                       output_fake=output_fake)

        # Log statistics for D once out of loop
        log_data.add_metric('errD', errD, group='loss')
        if self.fq_type:
            log_data.add_metric('errD_quant_real', quant_loss_real, group='loss_quant')
            log_data.add_metric('errD_quant_fake', quant_loss_fake, group='loss_quant')
        log_data.add_metric('D(x)', D_x, group='prob')
        log_data.add_metric('D(G(z))', D_Gz, group='prob')

        return log_data
