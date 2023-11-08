import math
import queue
import time
import unittest

from viztracer import VizTracer
import torch
import transformers

from impl.model.nn.flash_mqat import (FlashMQATForCausalLM, generate, GenerationConfig,
                                      InflightBatchingGenerator, PipeCacheData, PipeTransferData)
import api.huggingface


@unittest.skip("")
class PackedKVCacheTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.bs = bs = 4
        cls.device = device = "cuda"
        model_path = "/data/aigc/llm/checkpoints/4l-starcoder/"

        cls.tokenizer = api.huggingface.load_hf_tokenizer(model_path)

        cls.model = FlashMQATForCausalLM.from_starcoder(model_path=model_path,
                                                        dtype=torch.float16,
                                                        device=device)
        cls.model.eval()
        cls.config = cls.model.config

    @torch.no_grad()
    def testMain(self):
        input_lens = torch.randint(5, 10, (self.bs,), device=self.device)
        input_ids_list = [
            torch.randint(0, self.tokenizer.vocab_size, (input_len,), device=self.device)
            for input_len in input_lens
        ]

        # normal packed forward
        packed_input_ids = torch.cat(input_ids_list)
        max_seqlen = int(max(input_lens))
        cu_seqlens = torch.cat([input_lens.new_zeros(1), input_lens.cumsum(0)])

        x = PipeTransferData(cu_seqlens=cu_seqlens, max_seqlen=max_seqlen)
        ys = [PipeCacheData(input_ids=packed_input_ids)
              ] + [PipeCacheData() for _ in range(self.config.n_layers + 1)]
        logits1 = self.model(x, ys).pp_output
        logits1 = logits1[cu_seqlens[1:] - 1]

        # Use kv cache from the previous forward pass
        kv_cache_seqlen = 100
        _p = next(self.model.parameters())
        dtype, device = _p.dtype, _p.device
        k_caches = torch.zeros(
            (self.config.n_layers, self.bs, kv_cache_seqlen, self.config.n_kv_heads, self.config.head_dim),
            dtype=dtype,
            device=device,
        )
        v_caches = torch.zeros_like(k_caches)

        qlens = torch.randint(1, 4, (self.bs,), device=self.device)

        for layer_idx in range(self.config.n_layers):
            y = ys[1 + layer_idx]
            for i in range(self.bs):
                k_caches[layer_idx, i, :input_lens[i]] = y.k_cache[cu_seqlens[i]:cu_seqlens[i + 1]]
                v_caches[layer_idx, i, :input_lens[i]] = y.v_cache[cu_seqlens[i]:cu_seqlens[i + 1]]
            y.k_cache = k_caches[layer_idx]
            y.v_cache = v_caches[layer_idx]
            y.cache_seqlens = (input_lens - qlens).clone()
        ys[0].cache_seqlens = (input_lens - qlens).clone()

        q_cu_seqlens = torch.cat([qlens.new_zeros(1), qlens.cumsum(0)])
        x = PipeTransferData(cu_seqlens=q_cu_seqlens, max_seqlen=int(max(qlens)))
        q_input_ids_list = [x[-ql:] for x, ql in zip(input_ids_list, qlens)]
        q_packed_input_ids = torch.cat(q_input_ids_list)
        ys[0].input_ids = q_packed_input_ids
        ys[0].position_ids = None
        logits2 = self.model(x, ys).pp_output
        logits2 = logits2[q_cu_seqlens[1:] - 1]

        assert torch.allclose(logits1, logits2, atol=1e-2), (logits1, logits2)


class InflightBatchingGeneratorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.bs = bs = 4
        cls.device = device = "cuda"
        model_path = "/data/aigc/llm/checkpoints/4l-starcoder/"

        cls.tokenizer = api.huggingface.load_hf_tokenizer(model_path)

        cls.model = FlashMQATForCausalLM.from_starcoder(model_path=model_path,
                                                        dtype=torch.float16,
                                                        device=device)
        cls.model.eval()
        cls.config = cls.model.config

        cls.inqueue = queue.Queue(bs * 10)
        cls.outqueue = queue.Queue(bs * 10)
        cls.max_prompt_len = 10
        cls.gconfig = gconfig = GenerationConfig(min_new_tokens=1, max_new_tokens=10, greedy=True)
        cls.generator = InflightBatchingGenerator(
            inqueue=cls.inqueue,
            outqueue=cls.outqueue,
            model=cls.model,
            tokenizer=cls.tokenizer,
            gconfig=gconfig,
            batch_size=bs,
            max_prompt_len=cls.max_prompt_len,
        )

        # The unit of min duration is us (1e-6).
        cls.tracer = VizTracer(
            max_stack_depth=10,
            # ignore_c_function=False,
            min_duration=500,
            # include_files=['./impl/', './tests/'],
            ignore_frozen=True,
        )

    def testMain(self):
        prompts_str = [
            "I'm very happy today hahaha hahaha ",
            "Gues what's happening, ",
            "Coding is extremely exhausting",
            "Some random ",
            "Get more random words",
            "试试中文的prompt",
            "NVIDIA Nsight system is a very good tool for",
            "ghfjla hblka\n",
        ]
        encoding = self.tokenizer(
            prompts_str,
            padding=True,
            truncation=True,
            return_tensors="pt",
            return_length=True,
        )

        # burn-in run to warmup GPU
        generate(
            model=self.model,
            tokenizer=self.tokenizer,
            input_ids=encoding["input_ids"].cuda(),
            attention_mask=encoding["attention_mask"].cuda(),
            gconfig=self.gconfig,
        )

        self.tracer.start()
        tik = time.perf_counter()
        gen_tokens, logp, logits_mask, _, _ = generate(
            model=self.model,
            tokenizer=self.tokenizer,
            input_ids=encoding["input_ids"].cuda(),
            attention_mask=encoding["attention_mask"].cuda(),
            gconfig=self.gconfig,
        )
        t1 = time.perf_counter() - tik

        prompt = encoding["input_ids"]
        gen_lens = ((gen_tokens != self.tokenizer.pad_token_id).logical_and(
            gen_tokens != self.tokenizer.eos_token_id).sum(1))
        gen_lens = (gen_lens + 1).clip(max=gen_tokens.shape[1])
        prompt_lens = encoding["attention_mask"].sum(1)

        seqs = []
        logps = []
        for i in range(len(prompts_str)):
            p = prompt[i, :prompt_lens[i]]
            g = gen_tokens[i, :gen_lens[i]]
            seqs.append(torch.cat([p, g.cpu()]))
            logps.append(logp[i, :gen_lens[i]])

        # seqs_str = self.tokenizer.batch_decode(seqs, skip_special_tokens=True)

        encoding2 = self.tokenizer(prompts_str, padding=False, truncation=True)
        for p in encoding2["input_ids"]:
            self.inqueue.put(torch.tensor(p, device=self.device, dtype=torch.long))
        for _ in range(self.bs * 10 - len(prompts_str)):
            self.inqueue.put(torch.ones(5, dtype=torch.long, device=self.device))

        tik = time.perf_counter()
        all_res = []
        while len(all_res) < len(prompts_str):
            try:
                all_res.append(self.outqueue.get_nowait())
            except queue.Empty:
                pass
            self.generator.step_for(1)
        t2 = time.perf_counter() - tik

        print(t1, t2)

        seqs2 = []
        logps2 = []
        for res in all_res:
            seqs2.append(torch.cat([res["prompt"], res["gen"]]).cpu())
            logps2.append(res["logp"])

        for x in seqs:
            for y in seqs2:
                if x[0] == y[0]:
                    assert torch.allclose(x, y), (x, y)
        for x in logps:
            for y in logps2:
                if x[0] == y[0]:
                    assert torch.allclose(x, y), (x, y)
        self.tracer.stop()
        self.tracer.save(output_file='result.json')


if __name__ == "__main__":
    unittest.main()
