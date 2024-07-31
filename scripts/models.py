import torch
import torch.nn as nn
from nano_gpt import GPT2Model, GPT2Config, LayerNorm
from mamba import MambaConfig, Mamba
import math


MAX_NUM_CLASS = 2  # for openML classification task


def build_model(conf):
    if conf.family == "gpt2":
        model = TransformerModel(
            n_dims=conf.n_dims,
            n_positions=conf.n_positions,
            n_embd=conf.n_embd,
            n_layer=conf.n_layer,
            n_head=conf.n_head,
            pred_type=conf.pred_type,
        )
    elif conf.family == "gpt2_loop":
        model = TransformerModelLooped(
            n_dims=conf.n_dims,
            n_positions=conf.n_positions,
            n_embd=conf.n_embd,
            n_layer=conf.n_layer,
            n_head=conf.n_head,
            loop_func=conf.loop_func,
            pred_type=conf.pred_type,
            apply_input_mask=conf.apply_input_mask,
            p=conf.p,
            truncate_state=conf.truncate_state,
            p_state=conf.p_state,
            fixed_truncate=conf.fixed_truncate,
            tokens_to_trunc=conf.tokens_to_trunc,
        )
    elif conf.family == "gpt2_tying":
        model = TransformerModelTying(
            n_dims=conf.n_dims,
            n_positions=conf.n_positions,
            n_embd=conf.n_embd,
            n_layer=conf.n_layer,
            n_head=conf.n_head,
        )
    elif conf.family == "mamba":
        model = MambaModel(
            n_dims=conf.n_dims,
            n_positions=conf.n_positions,
            n_embd=conf.n_embd,
            n_layer=conf.n_layer,
            d_state=conf.d_state,
            expand=conf.expand,
            d_conv=conf.d_conv,
            pred_type=conf.pred_type,
        )
    elif conf.family == "mamba_loop":
        model = MambaModelLooped(
            n_dims=conf.n_dims,
            n_positions=conf.n_positions,
            n_embd=conf.n_embd,
            n_layer=conf.n_layer,
            d_state=conf.d_state,
            expand=conf.expand,
            d_conv=conf.d_conv,
            loop_func=conf.loop_func,
            pred_type=conf.pred_type,
        )
    else:
        raise NotImplementedError

    return model


class TransformerModel(nn.Module):
    def __init__(
        self,
        n_dims,
        n_positions,
        n_embd=128,
        n_layer=12,
        n_head=4,
        pred_type="regression",
    ):
        super(TransformerModel, self).__init__()
        self.freq = 2
        self.ind = 0
        configuration = GPT2Config()
        configuration.block_size = self.freq * n_positions + 1
        configuration.n_layer = n_layer
        configuration.n_head = n_head
        configuration.n_embd = n_embd
        configuration.dropout = 0.0
        configuration.bias = True
        configuration.dropout = 0.0
        self.configuration = configuration

        self.n_positions = n_positions  # n = points in this setting
        self.n_dims = n_dims  # input dimension, d_in
        self.n_embd = n_embd  # d
        self.n_layer = n_layer
        self._pred_type = pred_type

        self._read_in = nn.Linear(n_dims, n_embd)
        self._backbone = GPT2Model(self.configuration)
        if self._pred_type == "regression":
            self._read_out = nn.Linear(n_embd, 1)
        elif self._pred_type == "classification":
            self._read_out = nn.Linear(n_embd, MAX_NUM_CLASS)  # NOTE: hard-code

        self.print_flag = False

    def _combine(self, xs_b, ys_b):
        """
        :param xs_b: shape [B, n, d_in]
        :param ys_b: shape [B, n]
        :return: shape [B, 2n, d_in]
        """
        B, n, d = xs_b.shape
        device = xs_b.device

        ys_b_wide = torch.cat(
            (
                ys_b.view(B, n, 1),
                torch.zeros(B, n, d - 1, device=device),
            ),
            axis=2,
        )

        zs = torch.stack((xs_b, ys_b_wide), dim=2)
        zs = zs.view(B, self.freq * n, d)

        return zs

    def forward(self, xs, ys, add_inputs_embeds=False):
        """
        :param xs: [B, n, d]
        :param ys: [B, n]
        :return:
        """

        B, n, d_in = xs.shape
        zs = self._combine(xs, ys)  # [B, n, d_in], [B, n]-> [B, 2n, d_in]
        embeds = self._read_in(zs)  # [B, 2n, d_in] -> [B, 2n, d]

        f_output = self._backbone(
            inputs_embeds=embeds,
            position_ids=None,
            rm_pos_embd=False,
            add_inputs_embeds=add_inputs_embeds,
        )  # [B, 2n, d]
        prediction = self._read_out(f_output)  # [B, 2n, d] -> [B, 2n, 1]
        if self._pred_type == "regression":
            y = prediction[:, self.ind :: self.freq, 0]
        elif self._pred_type == "classification":
            y = prediction[:, self.ind :: self.freq]
        else:
            raise NotImplementedError

        return y


class TransformerModelTying(TransformerModel):
    def __init__(self, n_dims, n_positions, n_embd=128, n_layer=12, n_head=4):
        super(TransformerModelTying, self).__init__(
            n_dims, n_positions, n_embd, n_layer, n_head
        )

        self.configuration.n_layer = 1

        self._backbone = GPT2Model(self.configuration)

        self.print_flag = False

    def f(self, output):
        f_output = self._backbone(inputs_embeds=output)  # [B, 2n, d]
        return f_output

    def forward(self, xs, ys, add_inputs_embeds):
        """
        :param xs: [B, n, d]
        :param ys: [B, n]
        :param n_loop_start: int
        :param n_loops: int
        :return:
        """
        zs = self._combine(xs, ys)  # [B, n, d_in], [B, n], -> [B, 2n, d_in + 1]
        embeds = self._read_in(zs)  # [B, 2n, d_in + 1] -> [B, 2n, d]
        output = embeds  # also of shape [B, 2n, d]

        for idx in range(self.n_layer):
            output = self.f(output)
        prediction = self._read_out(output)  # [B, 2n, d] -> [B, 2n, 1]
        y = prediction[:, self.ind :: self.freq, 0]  # [B, n]

        return y


def dynamic_mask(tensor, p):
    mask = torch.rand_like(tensor) >= p
    return tensor * mask


class TransformerModelLooped(TransformerModel):
    def __init__(
        self,
        n_dims,
        n_positions,
        n_embd=128,
        n_layer=12,
        n_head=4,
        loop_func="z=f(x+z)",
        pred_type="regression",
        apply_input_mask=False,
        p=0.15, # part of the input to be masked
        truncate_state=False,
        p_state=0.3,  # part of the output state to be masked
        fixed_truncate=False,  # use fixed truncation instead of vector portion
        tokens_to_trunc=4,
    ):
        super(TransformerModelLooped, self).__init__(
            n_dims, n_positions, n_embd, n_layer, n_head, pred_type
        )
        self.loop_func = loop_func
        self.p = p
        self.apply_input_mask = apply_input_mask
        self.truncate_state = truncate_state
        self.p_state = p_state
        self.fixed_truncate = fixed_truncate
        self.tokens_to_trunc = tokens_to_trunc

    def f(self, output, embeds):
        """
        :param output: output state from the prev loop iteration [B, 2n, d]
        :param embeds: embeddings of the input [B, 2n, d]
        :return updated output state
        """
        # apply dynamic masking on the input tensor
        # if loop function is addition -> zero some elements
        # if loop function is multiplixation -> set some elements to 1 (not sure)
        if self.apply_input_mask:
            embeds = dynamic_mask(embeds, self.p)
            if self.loop_func == "z=f(x*z)":
                embeds = torch.where(embeds == 0, torch.ones_like(embeds), embeds)

        # mask part of the prev output state
        if self.truncate_state:
            B, n, d = output.shape
            # number of tokens to mask with dynamic masking
            if not self.fixed_truncate:
                self.tokens_to_trunc = math.ceil(n * self.p_state)
            mask = torch.ones((B, n, d), dtype=output.dtype, device=output.device)
            mask[:, : self.tokens_to_trunc, :] = 0
            output = output * mask

        if self.loop_func == "z=f(x+z)":
            f_output = self._backbone(inputs_embeds=output + embeds)  # [B, 2n, d]
        elif self.loop_func == "z=f(x*z)":
            f_output = self._backbone(inputs_embeds=output * embeds)  # [B, 2n, d]
        else:
            raise NotImplementedError
        return f_output

    def forward(self, xs, ys, n_loop_start, n_loops):
        """
        :param xs: [B, n, d]
        :param ys: [B, n]
        :param n_loop_start: int
        :param n_loops: int
        :return:
        """
        B, n, d_in = xs.shape
        zs = self._combine(xs, ys)  # [B, n, d_in], [B, n] -> [B, 2n, d_in]
        embeds = self._read_in(zs)  # [B, 2n, d_in] -> [B, 2n, d]
        if self.loop_func in ["z=f(x+z)"]:
            output = torch.zeros_like(embeds)  # also of shape [B, 2n, d]
        elif self.loop_func in ["z=f(x*z)"]:
            output = torch.ones_like(embeds)  # also of shape [B, 2n, d]
        else:
            raise NotImplementedError(
                "Currently we only support loop function z=f(x+z) or z=f(x*z)."
            )

        pred_list = []
        for idx in range(n_loops):
            if idx < n_loop_start:  # this will save memory when n_loops large.
                with torch.no_grad():
                    output = self.f(output, embeds)
            else:
                output = self.f(output, embeds)
                prediction = self._read_out(output)  # [B, 2n, d] -> [B, 2n, 1]
                if self._pred_type == "regression":
                    y = prediction[:, self.ind :: self.freq, 0]
                elif self._pred_type == "classification":
                    y = prediction[:, self.ind :: self.freq]
                else:
                    raise NotImplementedError
                pred_list.append(y)
            if not self.print_flag:
                print(idx)
                self.print_flag = True

        return pred_list


class MambaModel(nn.Module):
    def __init__(
        self,
        n_dims,
        n_positions,
        n_embd=128,
        n_layer=12,
        d_state=16,
        expand=2,
        d_conv=4,
        pred_type="regression",
    ):
        super(MambaModel, self).__init__()
        self.freq = 2
        self.ind = 0
        configuration = MambaConfig(
            n_embd=n_embd,
            n_layer=n_layer,
            d_state=d_state,
            expand=expand,
            dt_rank="auto",
            d_conv=d_conv,
        )
        self.configuration = configuration

        self.n_positions = n_positions  # n = points in this setting
        self.n_dims = n_dims  # input dimension, d_in
        self.n_embd = n_embd  # d
        self.n_layer = n_layer
        self._pred_type = pred_type

        self._read_in = nn.Linear(n_dims, n_embd)
        self._backbone = Mamba(self.configuration)
        if self._pred_type == "regression":
            self._read_out = nn.Linear(n_embd, 1)
        elif self._pred_type == "classification":
            self._read_out = nn.Linear(n_embd, MAX_NUM_CLASS)  # NOTE: hard-code

        self.print_flag = False

    def _combine(self, xs_b, ys_b):
        """
        :param xs_b: shape [B, n, d_in]
        :param ys_b: shape [B, n]
        :return: shape [B, 2n, d_in + 1]
        """
        B, n, d = xs_b.shape
        device = xs_b.device

        ys_b_wide = torch.cat(
            (
                ys_b.view(B, n, 1),
                torch.zeros(B, n, d - 1, device=device),
            ),
            axis=2,
        )

        zs = torch.stack((xs_b, ys_b_wide), dim=2)
        zs = zs.view(B, self.freq * n, d)

        return zs

    def forward(self, xs, ys, add_inputs_embeds=False):
        """
        :param xs: [B, n, d]
        :param ys: [B, n]
        :return:
        """

        B, n, d_in = xs.shape
        zs = self._combine(xs, ys)  # [B, n, d_in], [B, n] -> [B, 2n, d_in]
        embeds = self._read_in(zs)  # [B, 2n, d_in] -> [B, 2n, d]

        f_output = self._backbone(
            embeds,
        )  # [B, 2n, d]
        prediction = self._read_out(f_output)  # [B, 2n, d] -> [B, 2n, 1]
        if self._pred_type == "regression":
            y = prediction[:, self.ind :: self.freq, 0]
        elif self._pred_type == "classification":
            y = prediction[:, self.ind :: self.freq]
        else:
            raise NotImplementedError

        return y


class MambaModelLooped(MambaModel):
    def __init__(
        self,
        n_dims,
        n_positions,
        n_embd=128,
        n_layer=12,
        d_state=16,
        expand=2,
        d_conv=4,
        loop_func="z=f(x+z)",
        pred_type="regression",
    ):
        super(MambaModelLooped, self).__init__(
            n_dims, n_positions, n_embd, n_layer, d_state, expand, d_conv, pred_type
        )
        self.loop_func = loop_func

    def f(self, output, embeds):
        if self.loop_func == "z=f(x+z)":
            f_output = self._backbone(inputs_embeds=output + embeds)  # [B, 2n, d]
        elif self.loop_func == "z=f(x*z)":
            f_output = self._backbone(inputs_embeds=output * embeds)  # [B, 2n, d]
        else:
            raise NotImplementedError
        return f_output

    def forward(self, xs, ys, n_loop_start, n_loops):
        """
        :param xs: [B, n, d]
        :param ys: [B, n]
        :param n_loop_start: int - T from the paper
        :param n_loops: int - b from the paper
        :return:
        """
        B, n, d_in = xs.shape
        zs = self._combine(xs, ys)  # [B, n, d_in], [B, n] -> [B, 2n, d_in]
        embeds = self._read_in(zs)  # [B, 2n, d_in] -> [B, 2n, d]
        if self.loop_func in ["z=f(x+z)"]:
            output = torch.zeros_like(embeds)  # also of shape [B, 2n, d]
        elif self.loop_func in ["z=f(x*z)"]:
            output = torch.ones_like(embeds)  # also of shape [B, 2n, d]
        else:
            raise NotImplementedError(
                "Currently we only support loop function z=f(x+z) or z=f(x*z)."
            )

        pred_list = []
        for idx in range(n_loops):
            if idx < n_loop_start:  # this will save memory when n_loops large.
                with torch.no_grad():
                    output = self.f(output, embeds)
            else:
                output = self.f(output, embeds)
                prediction = self._read_out(output)  # [B, 2n, d] -> [B, 2n, 1]
                if self._pred_type == "regression":
                    y = prediction[:, self.ind :: self.freq, 0]
                elif self._pred_type == "classification":
                    y = prediction[:, self.ind :: self.freq]
                else:
                    raise NotImplementedError
                pred_list.append(y)
            if not self.print_flag:
                print(idx)
                self.print_flag = True

        return pred_list


##  TEST
if __name__ == "__main__":
    dim = 10
    pos = 20
    batch = 3
    T = 15
    b = 30
    model = MambaModel(dim, pos)
    model2 = TransformerModel(dim, pos)
    model3 = MambaModelLooped(dim, pos).to("cuda")
    xs = torch.rand((batch, pos, dim), device="cuda")
    ys = torch.rand((batch, pos), device="cuda")
    print(model(xs, ys))
    print(model2(xs, ys))
    print(model3(xs, ys, T, b))
