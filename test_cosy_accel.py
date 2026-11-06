"""Run with the CosyVoice interpreter; skipped where torch is not installed."""
from types import SimpleNamespace

import pytest

torch = pytest.importorskip("torch")

import cosy_server as server


def speaker(tokens):
    return {"flow_prompt_speech_token":torch.arange(tokens).reshape(1, -1),
            "flow_prompt_speech_token_len":torch.tensor([tokens]),
            "prompt_speech_feat":torch.arange(tokens * 2).reshape(1, -1, 1).repeat(1, 1, 80),
            "prompt_speech_feat_len":torch.tensor([tokens * 2]),
            "llm_prompt_speech_token":torch.arange(tokens).reshape(1, -1)}


def test_crop_keeps_aligned_reference_tail_for_flow_only():
    item = speaker(400)
    server.crop_flow_prompt(item, 5)
    assert item["flow_prompt_speech_token"].shape[1] == 125
    assert item["flow_prompt_speech_token"][0, 0] == 275
    assert item["prompt_speech_feat"].shape[1] == 250
    assert item["prompt_speech_feat"][0, 0, 0] == 550
    assert item["flow_prompt_speech_token_len"].item() == 125
    assert item["prompt_speech_feat_len"].item() == 250
    assert item["llm_prompt_speech_token"].shape[1] == 400


@pytest.mark.parametrize("tokens,seconds", [(100, 5), (400, 0)])
def test_crop_leaves_short_reference_or_disabled_setting_alone(tokens, seconds):
    item = speaker(tokens)
    server.crop_flow_prompt(item, seconds)
    assert item["flow_prompt_speech_token"].shape[1] == tokens


def test_prompt_too_long_for_graph_uses_vendor_decoder(monkeypatch):
    import cosy_accel
    calls = []
    class FakeDecoder:
        capacity = 100
        def __init__(self, lm, capacity):
            pass
        def generate(self, *args):
            calls.append("graph")
            yield 1
    def vendor(*args):
        calls.append("vendor")
        yield 2
    monkeypatch.setattr(cosy_accel, "GraphDecoder", FakeDecoder)
    lm = SimpleNamespace(inference_wrapper=vendor)
    cosy_accel.install_graph_decoder(lm)
    assert list(lm.inference_wrapper(torch.zeros(1, 90, 4), 25, 2, 10, "id")) == [1]
    assert list(lm.inference_wrapper(torch.zeros(1, 91, 4), 25, 2, 10, "id")) == [2]
    assert calls == ["graph", "vendor"]


@pytest.mark.skipif(not torch.cuda.is_available(), reason="CUDA graphs need a GPU")
def test_graph_step_matches_transformers_decoding():
    from transformers import Qwen2Config, Qwen2ForCausalLM
    import cosy_accel
    torch.manual_seed(0)
    config = Qwen2Config(hidden_size=64, intermediate_size=128, num_hidden_layers=2, num_attention_heads=4,
                         num_key_value_heads=2, vocab_size=32, rope_theta=1000000.0)
    causal = Qwen2ForCausalLM(config).cuda().eval()
    decoder_head = torch.nn.Linear(64, 40).cuda()
    embedding = torch.nn.Embedding(40, 64).cuda()

    def forward_one_step(inputs, masks, cache=None):
        output = causal(inputs_embeds=inputs, attention_mask=masks[:, -1, :], output_hidden_states=True,
                        return_dict=True, use_cache=True, past_key_values=cache)
        return output.hidden_states[-1], output.past_key_values

    lm = SimpleNamespace(llm=SimpleNamespace(model=causal, forward_one_step=forward_one_step),
                         llm_decoder=decoder_head, speech_embedding=embedding)
    graph = cosy_accel.GraphDecoder(lm, capacity=64)
    with torch.inference_mode():
        prompt = torch.randn(1, 9, 64, device="cuda")
        length = prompt.shape[1]
        masks = torch.ones((1, length, length), device="cuda").tril().bool()
        hidden, cache = forward_one_step(prompt, masks)
        expected = decoder_head(hidden[:, -1]).log_softmax(dim=-1)
        actual = graph.prefill(prompt)
        for step in range(12):
            assert torch.allclose(actual, expected, atol=1e-4)
            token = int(expected.argmax())
            length += 1
            masks = torch.ones((1, length, length), device="cuda").tril().bool()
            hidden, cache = forward_one_step(embedding.weight[token].reshape(1, 1, -1), masks, cache)
            expected = decoder_head(hidden[:, -1]).log_softmax(dim=-1)
            actual = graph.advance(token).clone()
