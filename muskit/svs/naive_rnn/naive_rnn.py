# Copyright 2021 Carnegie Mellon University (Jiatong Shi)
#  Apache 2.0  (http://www.apache.org/licenses/LICENSE-2.0)

"""Transformer-SVS related modules."""

from typing import Dict
from typing import Optional
from typing import Sequence
from typing import Tuple

import torch
import torch.nn.functional as F

import logging
from typeguard import check_argument_types

from muskit.torch_utils.nets_utils import make_non_pad_mask
from muskit.svs.bytesing.encoder import Encoder as EncoderPrenet
from muskit.svs.bytesing.decoder import Postnet
from muskit.layers.transformer.mask import subsequent_mask
from muskit.torch_utils.device_funcs import force_gatherable
from muskit.torch_utils.initialize import initialize
from muskit.svs.abs_svs import AbsSVS

import random
from torch.distributions import Beta

Beta_distribution = Beta(torch.tensor([0.5]), torch.tensor([0.5]))


class NaiveRNNLoss(torch.nn.Module):
    """Loss function module for Tacotron2."""

    def __init__(self, use_masking=True, use_weighted_masking=False):
        """Initialize Tactoron2 loss module.
        Args:
            use_masking (bool): Whether to apply masking
                for padded part in loss calculation.
            use_weighted_masking (bool):
                Whether to apply weighted masking in loss calculation.
        """
        super(NaiveRNNLoss, self).__init__()
        assert (use_masking != use_weighted_masking) or not use_masking
        self.use_masking = use_masking
        self.use_weighted_masking = use_weighted_masking

        # define criterions
        reduction = "none" if self.use_weighted_masking else "mean"
        self.l1_criterion = torch.nn.L1Loss(reduction=reduction)
        self.mse_criterion = torch.nn.MSELoss(reduction=reduction)

        # NOTE(kan-bayashi): register pre hook function for the compatibility
        # self._register_load_state_dict_pre_hook(self._load_state_dict_pre_hook)

    def forward(self, after_outs, before_outs, ys, olens):
        """Calculate forward propagation.
        Args:
            after_outs (Tensor): Batch of outputs after postnets (B, Lmax, odim).
            before_outs (Tensor): Batch of outputs before postnets (B, Lmax, odim).
            ys (Tensor): Batch of padded target features (B, Lmax, odim).
            olens (LongTensor): Batch of the lengths of each target (B,).
        Returns:
            Tensor: L1 loss value.
            Tensor: Mean square error loss value.
        """
        # make mask and apply it
        if self.use_masking:
            masks = make_non_pad_mask(olens).unsqueeze(-1).to(ys.device)
            ys = ys.masked_select(masks)
            after_outs = after_outs.masked_select(masks)
            before_outs = before_outs.masked_select(masks)

        # calculate loss
        l1_loss = self.l1_criterion(after_outs, ys) + self.l1_criterion(before_outs, ys)
        mse_loss = self.mse_criterion(after_outs, ys) + self.mse_criterion(
            before_outs, ys
        )

        # make weighted mask and apply it
        if self.use_weighted_masking:
            masks = make_non_pad_mask(olens).unsqueeze(-1).to(ys.device)
            weights = masks.float() / masks.sum(dim=1, keepdim=True).float()
            out_weights = weights.div(ys.size(0) * ys.size(2))

            # apply weight
            l1_loss = l1_loss.mul(out_weights).masked_select(masks).sum()
            mse_loss = mse_loss.mul(out_weights).masked_select(masks).sum()

        return l1_loss, mse_loss


class NaiveRNN(AbsSVS):
    """NaiveRNN-SVS module
    This is an implementation of naive RNN for singing voice synthesis
    The features are processed directly over time-domain from music score and
    predict the singing voice features
    """

    def __init__(
        self,
        # network structure related
        idim: int,
        midi_dim: int,
        odim: int,
        embed_dim: int = 512,
        eprenet_conv_layers: int = 3,
        eprenet_conv_chans: int = 256,
        eprenet_conv_filts: int = 5,
        elayers: int = 3,
        eunits: int = 1024,
        ebidirectional: bool = True,
        midi_embed_integration_type: str = "add",
        dlayers: int = 3,
        dunits: int = 1024,
        dbidirectional: bool = True,
        postnet_layers: int = 5,
        postnet_chans: int = 256,
        postnet_filts: int = 5,
        use_batch_norm: bool = True,
        reduction_factor: int = 1,
        # extra embedding related
        spks: Optional[int] = None,
        langs: Optional[int] = None,
        spk_embed_dim: Optional[int] = None,
        spk_embed_integration_type: str = "add",
        eprenet_dropout_rate: float = 0.5,
        edropout_rate: float = 0.1,
        ddropout_rate: float = 0.1,
        postnet_dropout_rate: float = 0.5,
        init_type: str = "xavier_uniform",
        use_masking: bool = False,
        use_weighted_masking: bool = False,
        loss_type: str = "L1",
        use_mixup_training: bool = False,
        loss_mixup_wight: float = 0.1,
    ):
        """Initialize NaiveRNN module.
        Args: TODO
        """
        assert check_argument_types()
        super().__init__()

        # store hyperparameters
        self.idim = idim
        self.midi_dim = midi_dim
        self.eunits = eunits
        self.odim = odim
        self.eos = idim - 1
        self.reduction_factor = reduction_factor
        self.loss_type = loss_type

        self.midi_embed_integration_type = midi_embed_integration_type

        # mixup - augmentation
        self.use_mixup_training = use_mixup_training
        self.loss_mixup_wight = loss_mixup_wight

        # use idx 0 as padding idx
        self.padding_idx = 0

        # define transformer encoder
        if eprenet_conv_layers != 0:
            # encoder prenet
            self.encoder_input_layer = torch.nn.Sequential(
                EncoderPrenet(
                    idim=idim,
                    embed_dim=embed_dim,
                    elayers=0,
                    econv_layers=eprenet_conv_layers,
                    econv_chans=eprenet_conv_chans,
                    econv_filts=eprenet_conv_filts,
                    use_batch_norm=use_batch_norm,
                    dropout_rate=eprenet_dropout_rate,
                    padding_idx=self.padding_idx,
                ),
                torch.nn.Linear(eprenet_conv_chans, eunits),
            )
            self.midi_encoder_input_layer = torch.nn.Sequential(
                EncoderPrenet(
                    idim=midi_dim,
                    embed_dim=embed_dim,
                    elayers=0,
                    econv_layers=eprenet_conv_layers,
                    econv_chans=eprenet_conv_chans,
                    econv_filts=eprenet_conv_filts,
                    use_batch_norm=use_batch_norm,
                    dropout_rate=eprenet_dropout_rate,
                    padding_idx=self.padding_idx,
                ),
                torch.nn.Linear(eprenet_conv_chans, eunits),
            )
        else:
            self.encoder_input_layer = torch.nn.Embedding(
                num_embeddings=idim, embedding_dim=eunits, padding_idx=self.padding_idx
            )
            self.midi_encoder_input_layer = torch.nn.Embedding(
                num_embeddings=midi_dim,
                embedding_dim=eunits,
                padding_idx=self.padding_idx,
            )

        self.encoder = torch.nn.LSTM(
            input_size=eunits,
            hidden_size=eunits,
            num_layers=elayers,
            batch_first=True,
            dropout=edropout_rate,
            bidirectional=ebidirectional,
            # proj_size=eunits,
        )

        self.midi_encoder = torch.nn.LSTM(
            input_size=eunits,
            hidden_size=eunits,
            num_layers=elayers,
            batch_first=True,
            dropout=edropout_rate,
            bidirectional=ebidirectional,
            # proj_size=eunits,
        )

        dim_direction = 2 if ebidirectional == True else 1
        if self.midi_embed_integration_type == "add":
            self.midi_projection = torch.nn.Linear(
                eunits * dim_direction, eunits * dim_direction
            )
        else:
            self.midi_projection = torch.nn.linear(
                2 * eunits * dim_direction, eunits * dim_direction
            )

        self.decoder = torch.nn.LSTM(
            input_size=eunits,
            hidden_size=eunits,
            num_layers=dlayers,
            batch_first=True,
            dropout=ddropout_rate,
            bidirectional=dbidirectional,
            # proj_size=dunits,
        )

        # define spk and lang embedding
        self.spks = None
        if spks is not None and spks > 1:
            self.spks = spks
            self.sid_emb = torch.nn.Embedding(spks, eunits)
        self.langs = None
        if langs is not None and langs > 1:
            # TODO (not encode yet)
            self.langs = langs
            self.lid_emb = torch.nn.Embedding(langs, eunits)

        # define projection layer
        self.spk_embed_dim = None
        if spk_embed_dim is not None and spk_embed_dim > 0:
            self.spk_embed_dim = spk_embed_dim
            self.spk_embed_integration_type = spk_embed_integration_type
        if self.spk_embed_dim is not None:
            if self.spk_embed_integration_type == "add":
                self.projection = torch.nn.Linear(self.spk_embed_dim, eunits)
            else:
                self.projection = torch.nn.Linear(eunits + self.spk_embed_dim, eunits)

        # define final projection
        self.feat_out = torch.nn.Linear(eunits * dim_direction, odim * reduction_factor)

        # define postnet
        self.postnet = (
            None
            if postnet_layers == 0
            else Postnet(
                idim=idim,
                odim=odim,
                n_layers=postnet_layers,
                n_chans=postnet_chans,
                n_filts=postnet_filts,
                use_batch_norm=use_batch_norm,
                dropout_rate=postnet_dropout_rate,
            )
        )

        # define loss function
        self.criterion = NaiveRNNLoss(
            use_masking=use_masking,
            use_weighted_masking=use_weighted_masking,
        )

        # initialize parameters
        self._reset_parameters(
            init_type=init_type,
        )

    def _reset_parameters(self, init_type):
        # initialize parameters
        if init_type != "pytorch":
            initialize(self, init_type)

    def forward(
        self,
        text: torch.Tensor,
        text_lengths: torch.Tensor,
        feats: torch.Tensor,
        feats_lengths: torch.Tensor,
        label: torch.Tensor,
        label_lengths: torch.Tensor,
        midi: torch.Tensor,
        midi_lengths: torch.Tensor,
        ds: torch.Tensor,
        tempo: Optional[torch.Tensor] = None,
        tempo_lengths: Optional[torch.Tensor] = None,
        spembs: Optional[torch.Tensor] = None,
        sids: Optional[torch.Tensor] = None,
        lids: Optional[torch.Tensor] = None,
        flag_IsValid=False,
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor], torch.Tensor]:
        """Calculate forward propagation.
        Args:
            text (LongTensor): Batch of padded character ids (B, Tmax).
            text_lengths (LongTensor): Batch of lengths of each input batch (B,).
            feats (Tensor): Batch of padded target features (B, Lmax, odim).
            feats_lengths (LongTensor): Batch of the lengths of each target (B,).
            label: (B, Tmax)
            label_lengths: (B,)
            midi: (B, Tmax)
            midi_lengths: (B,)
            spembs (Optional[Tensor]): Batch of speaker embeddings (B, spk_embed_dim).
            sids (Optional[Tensor]): Batch of speaker IDs (B, 1).
            lids (Optional[Tensor]): Batch of language IDs (B, 1).
        GS Fix:
            arguements from forward func. V.S. **batch from muskit_model.py
            label == durations ｜ phone sequence
            midi -> pitch sequence
        Returns:
            Tensor: Loss scalar value.
            Dict: Statistics to be monitored.
            Tensor: Weight value if not joint training else model outputs.
        """
        text = text[:, : text_lengths.max()]  # for data-parallel
        feats = feats[:, : feats_lengths.max()]  # for data-parallel
        midi = midi[:, : midi_lengths.max()]  # for data-parallel
        label = label[:, : label_lengths.max()]  # for data-parallel
        batch_size = feats.size(0)

        label_emb = self.encoder_input_layer(label)  # FIX ME: label Float to Int
        midi_emb = self.midi_encoder_input_layer(midi)

        if self.use_mixup_training and flag_IsValid == False:
            # batch_size_mixup = batch_size // 2                  # !!! NOTE: origin - 2
            # lst = [i for i in range(batch_size_mixup * 2)]
            # random.shuffle(lst)

            batch_size_mixup = 2
            lst = random.sample(
                [i for i in range(batch_size)], batch_size_mixup * 2
            )  # mix-up per 2 samples

            # mix-up augmentation
            feats_mixup = torch.zeros(
                (batch_size_mixup, feats.shape[1], feats.shape[2]),
                dtype=feats.dtype,
                layout=feats.layout,
                device=feats.device,
            )
            feats_lengths_mixup = torch.zeros(
                batch_size_mixup,
                dtype=feats_lengths.dtype,
                layout=feats_lengths.layout,
                device=feats_lengths.device,
            )

            midi_embed_mixup = torch.zeros(
                (batch_size_mixup, midi_emb.shape[1], midi_emb.shape[2]),
                dtype=midi_emb.dtype,
                layout=midi_emb.layout,
                device=midi_emb.device,
            )
            midi_lengths_mixup = torch.zeros(
                batch_size_mixup,
                dtype=midi_lengths.dtype,
                layout=midi_lengths.layout,
                device=midi_lengths.device,
            )

            label_embed_mixup = torch.zeros(
                (batch_size_mixup, label_emb.shape[1], label_emb.shape[2]),
                dtype=label_emb.dtype,
                layout=label_emb.layout,
                device=label_emb.device,
            )
            label_lengths_mixup = torch.zeros(
                batch_size_mixup,
                dtype=label_lengths.dtype,
                layout=label_lengths.layout,
                device=label_lengths.device,
            )

            for i in range(batch_size_mixup):
                index1 = lst[2 * i]
                index2 = lst[2 * i + 1]

                w1 = Beta_distribution.sample().to(
                    feats.device
                )  # !!! NOTE:  random.random()
                w2 = 1 - w1

                feats_mixup[i] = w1 * feats[index1] + w2 * feats[index2]
                feats_lengths_mixup[i] = max(
                    feats_lengths[index1], feats_lengths[index2]
                )

                midi_embed_mixup[i] = w1 * midi_emb[index1] + w2 * midi_emb[index2]
                midi_lengths_mixup[i] = max(midi_lengths[index1], midi_lengths[index2])

                label_embed_mixup[i] = w1 * label_emb[index1] + w2 * label_emb[index2]
                label_lengths_mixup[i] = max(
                    label_lengths[index1], label_lengths[index2]
                )

            feats = torch.cat((feats, feats_mixup), 0)
            feats_lengths = torch.cat((feats_lengths, feats_lengths_mixup), 0)
            midi_emb = torch.cat((midi_emb, midi_embed_mixup), 0)
            midi_lengths = torch.cat((midi_lengths, midi_lengths_mixup), 0)
            label_emb = torch.cat((label_emb, label_embed_mixup), 0)
            label_lengths = torch.cat((label_lengths, label_lengths_mixup), 0)
            batch_size_origin = batch_size
            batch_size = feats.size(0)

        label_emb = torch.nn.utils.rnn.pack_padded_sequence(
            label_emb, label_lengths.to("cpu"), batch_first=True, enforce_sorted=False
        )
        midi_emb = torch.nn.utils.rnn.pack_padded_sequence(
            midi_emb, midi_lengths.to("cpu"), batch_first=True, enforce_sorted=False
        )

        hs_label, (_, _) = self.encoder(label_emb)
        hs_midi, (_, _) = self.midi_encoder(midi_emb)

        hs_label, _ = torch.nn.utils.rnn.pad_packed_sequence(hs_label, batch_first=True)
        hs_midi, _ = torch.nn.utils.rnn.pad_packed_sequence(hs_midi, batch_first=True)

        if self.midi_embed_integration_type == "add":
            hs = hs_label + hs_midi
            hs = F.leaky_relu(self.midi_projection(hs))
        else:
            hs = torch.cat((hs_label, hs_midi), dim=-1)
            hs = F.leaky_relu(self.midi_projection(hs))
        # integrate spk & lang embeddings
        if self.spks is not None:
            sid_embs = self.sid_emb(sids.view(-1))
            hs = hs + sid_embs.unsqueeze(1)
        if self.langs is not None:
            lid_embs = self.lid_emb(lids.view(-1))
            hs = hs + lid_embs.unsqueeze(1)

        # integrate speaker embedding
        if self.spk_embed_dim is not None:
            hs = self._integrate_with_spk_embed(hs, spembs)

        # hs_emb = torch.nn.utils.rnn.pack_padded_sequence(
        #     hs, label_lengths.to("cpu"), batch_first=True, enforce_sorted=False
        # )

        # zs, (_, _) = self.decoder(hs_emb)
        # zs, _ = torch.nn.utils.rnn.pad_packed_sequence(zs, batch_first=True)

        # zs = zs[:, self.reduction_factor - 1 :: self.reduction_factor]

        # (B, T_feats//r, odim * r) -> (B, T_feats//r * r, odim)
        # before_outs = F.leaky_relu(self.feat_out(zs).view(zs.size(0), -1, self.odim))
        before_outs = F.leaky_relu(self.feat_out(hs).view(hs.size(0), -1, self.odim))

        # postnet -> (B, T_feats//r * r, odim)
        if self.postnet is None:
            after_outs = before_outs
        else:
            after_outs = before_outs + self.postnet(
                before_outs.transpose(1, 2)
            ).transpose(1, 2)

        # modifiy mod part of groundtruth
        if self.reduction_factor > 1:
            assert feats_lengths.ge(
                self.reduction_factor
            ).all(), "Output length must be greater than or equal to reduction factor."
            olens = feats_lengths.new(
                [olen - olen % self.reduction_factor for olen in feats_lengths]
            )
            max_olen = max(olens)
            ys = feats[:, :max_olen]
        else:
            ys = feats
            olens = feats_lengths

        # calculate loss values
        if self.use_mixup_training and flag_IsValid == False:
            olens = feats_lengths[:batch_size_origin]
            l1_loss_origin, l2_loss_origin = self.criterion(
                after_outs[:batch_size_origin, : olens.max()],
                before_outs[:batch_size_origin, : olens.max()],
                feats[:batch_size_origin],
                feats_lengths[:batch_size_origin],
            )
            olens = feats_lengths[batch_size_origin:batch_size]
            # logging.info(f"olens: {olens}, feats_lengths: {feats_lengths}")
            # logging.info(f"after_outs: {after_outs.shape}")
            # logging.info(f"feats: {feats.shape}")
            # logging.info(f"feats[batch_size_origin : batch_size]: {feats[batch_size_origin : batch_size].shape}")
            l1_loss_mixup, l2_loss_mixup = self.criterion(
                after_outs[batch_size_origin:batch_size, : olens.max()],
                before_outs[batch_size_origin:batch_size, : olens.max()],
                feats[batch_size_origin:batch_size, : olens.max()],
                feats_lengths[batch_size_origin:batch_size],
            )
            l1_loss = (
                1 - self.loss_mixup_wight
            ) * l1_loss_origin + self.loss_mixup_wight * l1_loss_mixup
            l2_loss = (
                1 - self.loss_mixup_wight
            ) * l2_loss_origin + self.loss_mixup_wight * l2_loss_mixup
        else:
            l1_loss, l2_loss = self.criterion(
                after_outs[:, : olens.max()], before_outs[:, : olens.max()], ys, olens
            )

        if self.loss_type == "L1":
            loss = l1_loss
        elif self.loss_type == "L2":
            loss = l2_loss
        elif self.loss_type == "L1+L2":
            loss = l1_loss + l2_loss
        else:
            raise ValueError("unknown --loss-type " + self.loss_type)

        stats = dict(
            loss=loss.item(),
            l1_loss=l1_loss.item(),
            l2_loss=l2_loss.item(),
        )
        if self.use_mixup_training and flag_IsValid == False:
            stats.update(l1_loss_mixup=l1_loss_mixup)

        loss, stats, weight = force_gatherable((loss, stats, batch_size), loss.device)

        if flag_IsValid == False:
            # training stage
            return loss, stats, weight
        else:
            # validation stage
            return loss, stats, weight, after_outs[:, : olens.max()], ys, olens

    def inference(
        self,
        text: torch.Tensor,
        label: torch.Tensor,
        midi: torch.Tensor,
        ds: torch.Tensor,
        feats: torch.Tensor = None,
        tempo: Optional[torch.Tensor] = None,
        spembs: Optional[torch.Tensor] = None,
        sids: Optional[torch.Tensor] = None,
        lids: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, Dict[str, torch.Tensor], torch.Tensor]:
        """Calculate forward propagation.
        Args:
            text (LongTensor): Batch of padded character ids (Tmax).
            label (Tensor)
            midi (Tensor)
            feats (Tensor): Batch of padded target features (Lmax, odim).
            spembs (Optional[Tensor]): Batch of speaker embeddings (spk_embed_dim).
            sids (Optional[Tensor]): Batch of speaker IDs (1).
            lids (Optional[Tensor]): Batch of language IDs (1).

        Returns:
            Dict[str, Tensor]: Output dict including the following items:
                * feat_gen (Tensor): Output sequence of features (T_feats, odim).
        """
        label_emb = self.encoder_input_layer(label)
        midi_emb = self.midi_encoder_input_layer(midi)

        hs_label, (_, _) = self.encoder(label_emb)
        hs_midi, (_, _) = self.midi_encoder(midi_emb)

        if self.midi_embed_integration_type == "add":
            hs = hs_label + hs_midi
            hs = F.leaky_relu(self.midi_projection(hs))
        else:
            hs = torch.cat((hs_label, hs_midi), dim=-1)
            hs = F.leaky_relu(self.midi_projection(hs))
        # integrate spk & lang embeddings
        if self.spks is not None:
            sid_embs = self.sid_emb(sids.view(-1))
            hs = hs + sid_embs.unsqueeze(1)
        if self.langs is not None:
            lid_embs = self.lid_emb(lids.view(-1))
            hs = hs + lid_embs.unsqueeze(1)

        # integrate speaker embedding
        if self.spk_embed_dim is not None:
            hs = self._integrate_with_spk_embed(hs, spembs)

        # (B, T_feats//r, odim * r) -> (B, T_feats//r * r, odim)
        before_outs = F.leaky_relu(self.feat_out(hs).view(hs.size(0), -1, self.odim))

        # postnet -> (B, T_feats//r * r, odim)
        if self.postnet is None:
            after_outs = before_outs
        else:
            after_outs = before_outs + self.postnet(
                before_outs.transpose(1, 2)
            ).transpose(1, 2)

        return after_outs, None, None  # outs, probs, att_ws

    def _integrate_with_spk_embed(
        self, hs: torch.Tensor, spembs: torch.Tensor
    ) -> torch.Tensor:
        """Integrate speaker embedding with hidden states.
        Args:
            hs (Tensor): Batch of hidden state sequences (B, Tmax, adim).
            spembs (Tensor): Batch of speaker embeddings (B, spk_embed_dim).
        Returns:
            Tensor: Batch of integrated hidden state sequences (B, Tmax, adim).
        """
        if self.spk_embed_integration_type == "add":
            # apply projection and then add to hidden states
            spembs = self.projection(F.normalize(spembs))
            hs = hs + spembs.unsqueeze(1)
        elif self.spk_embed_integration_type == "concat":
            # concat hidden states with spk embeds and then apply projection
            spembs = F.normalize(spembs).unsqueeze(1).expand(-1, hs.size(1), -1)
            hs = self.projection(torch.cat([hs, spembs], dim=-1))
        else:
            raise NotImplementedError("support only add or concat.")

        return hs
