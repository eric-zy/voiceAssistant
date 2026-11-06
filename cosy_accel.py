"""Runtime speedups for CosyVoice2 on a GPU that also drives the desktop.

Applied from cosy_server.py so vendor/CosyVoice stays an unmodified checkout.
"""
import math

import torch

# Fits a 30 s reference plus an 80-character chunk at the model's 20 tokens/char limit.
DEFAULT_CONTEXT_TOKENS = 2560


class GraphDecoder:
    """Replays one Qwen2 decode step as a CUDA graph.

    A step through transformers issues ~700 kernels from Python, which costs
    ~35 ms on Windows while the weights need ~10 ms to read. Prefill still uses
    the original forward; only the single-token step is replaced.
    """

    def __init__(self, lm, capacity=DEFAULT_CONTEXT_TOKENS):
        self.lm = lm
        self.capacity = capacity
        causal_lm = lm.llm.model
        self.body = causal_lm.model
        config = causal_lm.config
        self.heads = config.num_attention_heads
        self.kv_heads = config.num_key_value_heads
        self.head_dim = config.hidden_size // self.heads
        self.group = self.heads // self.kv_heads
        self.scale = 1 / math.sqrt(self.head_dim)
        weight = lm.llm_decoder.weight
        device, dtype = weight.device, weight.dtype
        layers = len(self.body.layers)
        shape = (layers, self.kv_heads, capacity, self.head_dim)
        self.keys = torch.zeros(shape, device=device, dtype=dtype)
        self.values = torch.zeros(shape, device=device, dtype=dtype)
        self.slots = torch.arange(capacity, device=device)
        self.position = torch.zeros(1, device=device, dtype=torch.long)
        self.embedding = torch.zeros(1, 1, config.hidden_size, device=device, dtype=dtype)
        self.logp = torch.zeros(1, lm.llm_decoder.out_features, device=device, dtype=dtype)
        half = torch.arange(0, self.head_dim, 2, device=device, dtype=torch.float32) / self.head_dim
        angles = torch.outer(torch.arange(capacity, device=device, dtype=torch.float32),
                             1.0 / config.rope_theta ** half)
        angles = torch.cat((angles, angles), dim=-1)
        self.cos, self.sin = angles.cos().to(dtype), angles.sin().to(dtype)
        self.graph = self._capture()

    def _rotate(self, states, cos, sin):
        first, second = states.chunk(2, dim=-1)
        return states * cos + torch.cat((-second, first), dim=-1) * sin

    def _step(self):
        cos, sin = self.cos.index_select(0, self.position), self.sin.index_select(0, self.position)
        hidden_mask = torch.where(self.slots <= self.position, 0.0, -math.inf).to(self.keys.dtype)
        hidden = self.embedding
        for index, layer in enumerate(self.body.layers):
            attention = layer.self_attn
            normed = layer.input_layernorm(hidden)
            query = self._rotate(attention.q_proj(normed).view(self.heads, self.head_dim), cos, sin)
            key = self._rotate(attention.k_proj(normed).view(self.kv_heads, self.head_dim), cos, sin)
            value = attention.v_proj(normed).view(self.kv_heads, self.head_dim)
            self.keys[index].index_copy_(1, self.position, key.unsqueeze(1))
            self.values[index].index_copy_(1, self.position, value.unsqueeze(1))
            grouped = query.view(self.kv_heads, self.group, self.head_dim)
            scores = torch.bmm(grouped, self.keys[index].transpose(1, 2)) * self.scale + hidden_mask
            mixed = torch.bmm(scores.softmax(dim=-1), self.values[index])
            hidden = hidden + attention.o_proj(mixed.reshape(1, 1, -1))
            hidden = hidden + layer.mlp(layer.post_attention_layernorm(hidden))
        self.logp.copy_(self.lm.llm_decoder(self.body.norm(hidden)[:, -1]).log_softmax(dim=-1))
        self.position.add_(1)

    def _capture(self):
        side = torch.cuda.Stream()
        side.wait_stream(torch.cuda.current_stream())
        with torch.inference_mode(), torch.cuda.stream(side):
            for _ in range(2):
                self._step()
        torch.cuda.current_stream().wait_stream(side)
        graph = torch.cuda.CUDAGraph()
        with torch.inference_mode(), torch.cuda.graph(graph):
            self._step()
        return graph

    def prefill(self, lm_input):
        """Run the prompt through the original model and return first-token log probabilities."""
        length = lm_input.shape[1]
        masks = torch.tril(torch.ones((1, length, length), device=lm_input.device)).to(torch.bool)
        hidden, cache = self.lm.llm.forward_one_step(lm_input, masks=masks, cache=None)
        for index in range(len(self.body.layers)):
            key, value = cache[index]
            self.keys[index, :, :length].copy_(key[0])
            self.values[index, :, :length].copy_(value[0])
        self.position.fill_(length)
        return self.lm.llm_decoder(hidden[:, -1]).log_softmax(dim=-1)

    def advance(self, token):
        self.embedding.copy_(self.lm.speech_embedding.weight[token].reshape(1, 1, -1))
        self.graph.replay()
        return self.logp

    def generate(self, lm_input, sampling, min_len, max_len):
        logp = self.prefill(lm_input)
        tokens = []
        for step in range(max_len):
            # Vendor sampling loops over GPU scalars; one copy keeps it on the CPU.
            scores = logp.squeeze(dim=0).cpu()
            token = self.lm.sampling_ids(scores, tokens, sampling, ignore_eos=step < min_len)
            if token in self.lm.stop_token_ids:
                return
            yield token
            tokens.append(token)
            logp = self.advance(token)


def install_graph_decoder(lm, capacity=DEFAULT_CONTEXT_TOKENS):
    """Route normal requests through the graph; oversized ones keep the vendor path."""
    decoder = GraphDecoder(lm, capacity)
    original = lm.inference_wrapper

    @torch.inference_mode()
    def inference_wrapper(lm_input, sampling, min_len, max_len, uuid):
        if lm_input.shape[1] + max_len > decoder.capacity:
            yield from original(lm_input, sampling, min_len, max_len, uuid)
        else:
            yield from decoder.generate(lm_input, sampling, min_len, max_len)

    lm.inference_wrapper = inference_wrapper
    return decoder


def tune_runtime(model, *, use_graph=True, vram_limit_gb=3.7):
    """Apply GPU settings once after the model has loaded."""
    if not torch.cuda.is_available():
        return
    # Without a cap the allocator's cached blocks fill all 6 GB on long text even
    # though live tensors need ~3 GB. At the cap it frees them and retries.
    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(min(1.0, vram_limit_gb * 2**30 / total))
    if use_graph:
        install_graph_decoder(model.model.llm)
