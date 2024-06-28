import numpy as np
from onnx import helper, onnx_pb as onnx_proto
from onnxruntime_extensions import make_onnx_model
import onnxruntime as _ort
import torch
from einops import rearrange, repeat
import math
import unittest

def construct_local_mask(
    seqlen_q,
    seqlen_k,
    window_size=(-1, -1),  # -1 means infinite window size
    query_padding_mask=None,
    key_padding_mask=None,
    device=None,
):
    row_idx = rearrange(torch.arange(seqlen_q, device=device, dtype=torch.long), "s -> s 1")
    col_idx = torch.arange(seqlen_k, device=device, dtype=torch.long)
    sk = seqlen_k if key_padding_mask is None else rearrange(key_padding_mask.sum(-1), "b -> b 1 1 1")
    sq = seqlen_q if query_padding_mask is None else rearrange(query_padding_mask.sum(-1), "b -> b 1 1 1")
    if window_size[0] < 0:
        return col_idx > row_idx + sk - sq + window_size[1]
    else:
        sk = torch.full_like(col_idx, seqlen_k) if key_padding_mask is None else sk
        return torch.logical_or(
            col_idx > torch.minimum(row_idx + sk - sq + window_size[1], sk),
            col_idx < row_idx + sk - sq - window_size[0],
        )

def attention_ref(
    q,
    k,
    v,
    query_padding_mask=None,
    key_padding_mask=None,
    dropout_p=0.0,
    dropout_mask=None,
    causal=False,
    window_size=(-1, -1),  # -1 means infinite window size
    upcast=True,
    reorder_ops=False,
):
    """
    Arguments:
        q: (batch_size, seqlen_q, nheads, head_dim)
        k: (batch_size, seqlen_k, nheads_k, head_dim)
        v: (batch_size, seqlen_k, nheads_k, head_dim)
        query_padding_mask: (batch_size, seqlen_q)
        key_padding_mask: (batch_size, seqlen_k)
        dropout_p: float
        dropout_mask: (batch_size, nheads, seqlen_q, seqlen_k)
        causal: whether to apply causal masking
        window_size: (int, int), left and right window size
        upcast: whether to cast all inputs to fp32, do all computation in fp32, then cast
            output back to fp16/bf16.
        reorder_ops: whether to change the order of operations (scaling k instead of scaling k, etc.)
            without changing the math. This is to estimate the numerical error from operation
            reordering.
    Output:
        output: (batch_size, seqlen_q, nheads, head_dim)
        attention: (batch_size, nheads, seqlen_q, seqlen_k), softmax after dropout
    """
    if causal:
        window_size = (window_size[0], 0)
    dtype_og = q.dtype
    if upcast:
        q, k, v = q.float(), k.float(), v.float()
    seqlen_q, seqlen_k = q.shape[1], k.shape[1]
    k = repeat(k, "b s h d -> b s (h g) d", g=q.shape[2] // k.shape[2])
    v = repeat(v, "b s h d -> b s (h g) d", g=q.shape[2] // v.shape[2])
    d = q.shape[-1]
    if not reorder_ops:
        scores = torch.einsum("bthd,bshd->bhts", q / math.sqrt(d), k)
    else:
        scores = torch.einsum("bthd,bshd->bhts", q, k / math.sqrt(d))
    if key_padding_mask is not None:
        scores.masked_fill_(rearrange(~key_padding_mask, "b s -> b 1 1 s"), float("-inf"))
    if window_size[0] >= 0 or window_size[1] >= 0:
        local_mask = construct_local_mask(
            seqlen_q,
            seqlen_k,
            window_size,
            query_padding_mask,
            key_padding_mask,
            q.device,
        )
        scores.masked_fill_(local_mask, float("-inf"))
    attention = torch.softmax(scores, dim=-1)
    # Some rows might be completely masked out so we fill them with zero instead of NaN
    if window_size[0] >= 0 or window_size[1] >= 0:
        attention = attention.masked_fill(torch.all(local_mask, dim=-1, keepdim=True), 0.0)
    # We want to mask here so that the attention matrix doesn't have any NaNs
    # Otherwise we'll get NaN in dV
    if query_padding_mask is not None:
        attention = attention.masked_fill(rearrange(~query_padding_mask, "b s -> b 1 s 1"), 0.0)
    dropout_scaling = 1.0 / (1 - dropout_p)
    if dropout_mask is not None:
        attention_drop = attention.masked_fill(~dropout_mask, 0.0)
    else:
        attention_drop = attention
    output = torch.einsum("bhts,bshd->bthd", attention_drop, v * dropout_scaling)
    if query_padding_mask is not None:
        output.masked_fill_(rearrange(~query_padding_mask, "b s -> b s 1 1"), 0.0)
    return output.to(dtype=dtype_og), attention.to(dtype=dtype_og)

def create_pagedattention_test_model(batch_size, slot_cnt_per_block, block_cnt_per_layer, block_cnt_needed_by_longest_seq, num_heads=32, num_kv_heads=32, head_size=16, scale=0.0, domain='onnx.genai', cos_sin_cache=False, positions=False):
    inputs = ['query', 'key', 'value', 'key_cache', 'value_cache', 'block_tables', 'slot_mappings', 'context_lens', 'is_prompt']
    if cos_sin_cache:
        inputs += ['cos_sin_cache']
    if positions:
        inputs += ['positions']
    nodes = [
        helper.make_node('PagedAttention',  
            inputs, 
            ['attn_out'], 
            domain=domain, num_heads=num_heads, num_kv_heads=num_kv_heads, head_size=head_size, scale=scale)
    ]
    query = helper.make_tensor_value_info(
        'query', onnx_proto.TensorProto.FLOAT16, [None, num_heads * head_size])
    key = helper.make_tensor_value_info(
        'key', onnx_proto.TensorProto.FLOAT16, [None, num_kv_heads * head_size])
    value = helper.make_tensor_value_info(
        'value', onnx_proto.TensorProto.FLOAT16, [None, num_kv_heads * head_size])
    key_cache = helper.make_tensor_value_info(
        'key_cache', onnx_proto.TensorProto.FLOAT16, [block_cnt_per_layer, num_kv_heads * head_size * slot_cnt_per_block])
    value_cache = helper.make_tensor_value_info(
        'value_cache', onnx_proto.TensorProto.FLOAT16, [block_cnt_per_layer, num_kv_heads * head_size * slot_cnt_per_block])
    block_tables = helper.make_tensor_value_info(
        'block_tables', onnx_proto.TensorProto.INT32, [batch_size, block_cnt_needed_by_longest_seq])
    slot_mappings = helper.make_tensor_value_info(
        'slot_mappings', onnx_proto.TensorProto.INT32, [None])
    context_lens = helper.make_tensor_value_info(
        'context_lens', onnx_proto.TensorProto.INT32, [batch_size])
    is_prompt = helper.make_tensor_value_info(
        'is_prompt', onnx_proto.TensorProto.INT32, [1])
    attn_out = helper.make_tensor_value_info(
        'attn_out', onnx_proto.TensorProto.FLOAT16, [None, num_heads * head_size])
    graph_input = [query, key, value, key_cache, value_cache, block_tables, slot_mappings, context_lens, is_prompt]
    if cos_sin_cache:
        cos_sin_cache = helper.make_tensor_value_info(
            'cos_sin_cache', onnx_proto.TensorProto.FLOAT16, [None, None])
        graph_input += [cos_sin_cache]
    if positions:
        positions = helper.make_tensor_value_info(
            'positions', onnx_proto.TensorProto.INT32, [None])
        graph_input += [positions]

    graph = helper.make_graph(nodes, 'test_paged_attention', 
                graph_input, 
                [attn_out])
    model = make_onnx_model(graph)
    return model

def create_ort_session(custom_op_shared_lib_path, batch_size, slot_cnt_per_block, block_cnt_per_layer, block_cnt_needed_by_longest_seq, num_heads=32, num_kv_heads=32, head_size=16, scale=0.0, cos_sin_cache=False, positions=False):
    so = _ort.SessionOptions()
    so.register_custom_ops_library(custom_op_shared_lib_path)
    onnx_model = create_pagedattention_test_model(batch_size, slot_cnt_per_block, block_cnt_per_layer, block_cnt_needed_by_longest_seq, num_heads, num_kv_heads, head_size, scale, "onnx.genai", cos_sin_cache, positions)
    return _ort.InferenceSession(onnx_model.SerializeToString(), so, providers=['CUDAExecutionProvider'])

#def test_cuda_paged_attention_prompt_decoding():
#    so = _ort.SessionOptions()
#    so.register_custom_ops_library('/home/leca/code/onnxruntime-genai/test/custom_ops/build/libgenai_custom_ops_test.so')
#    onnx_model = create_pagedattention_test_model(3, 16, 32, 8)
#    sess = _ort.InferenceSession(onnx_model.SerializeToString(),
#                                 so,
#                                 providers=['CUDAExecutionProvider'])
#
#    query = np.random.randn(381,512).astype(np.float16) # 381 is the token num of all the sequences (127, 127, 127)
#    key = np.random.randn(381,512).astype(np.float16)
#    value = np.random.randn(381,512).astype(np.float16)
#    key_cache = np.zeros([32,8192]).astype(np.float16)
#    value_cache = np.zeros([32,8192]).astype(np.float16)
#    block_tables = np.array([[0,1,2,3,4,5,6,7],[8,9,10,11,12,13,14,15],[16,17,18,19,20,21,22,23]]).astype(np.int32) # each sequence occupies 8 blocks (127/16)
#    slot1 = np.arange(0, 127, dtype=np.int32)
#    slot2 = np.arange(128, 255, dtype=np.int32)
#    slot3 = np.arange(256, 383, dtype=np.int32)
#    slot_mappings = np.concatenate((slot1, slot2, slot3))
#    context_lens = np.array([127, 127, 127]).astype(np.int32)
#    is_prompt = np.array([1]).astype(np.int32)
#
#    key_cache_ort = _ort.OrtValue.ortvalue_from_numpy(key_cache, "cuda")
#    value_cache_ort = _ort.OrtValue.ortvalue_from_numpy(value_cache, "cuda")
#    block_tables_ort = _ort.OrtValue.ortvalue_from_numpy(block_tables, "cuda")
#    slot_mappings_ort = _ort.OrtValue.ortvalue_from_numpy(slot_mappings, "cuda")
#    context_lens_ort = _ort.OrtValue.ortvalue_from_numpy(context_lens)
#    is_prompt_ort = _ort.OrtValue.ortvalue_from_numpy(is_prompt)
#
#    # prompt case
#    io_binding = sess.io_binding()
#    io_binding.bind_cpu_input("query", query)
#    io_binding.bind_cpu_input("key", key)
#    io_binding.bind_cpu_input("value", value)
#    io_binding.bind_ortvalue_input("key_cache", key_cache_ort)
#    io_binding.bind_ortvalue_input("value_cache", value_cache_ort)
#    io_binding.bind_ortvalue_input("block_tables", block_tables_ort)
#    io_binding.bind_ortvalue_input("slot_mappings", slot_mappings_ort)
#    io_binding.bind_ortvalue_input("context_lens", context_lens_ort)
#    io_binding.bind_ortvalue_input("is_prompt", is_prompt_ort)
#    io_binding.bind_output("attn_out")
#    sess.run_with_iobinding(io_binding)
#
#    # decoding case
#    query2 = np.random.randn(3, 512).astype(np.float16)
#    key2 = np.random.randn(3, 512).astype(np.float16)
#    value2 = np.random.randn(3, 512).astype(np.float16)
#    slot = np.array([127, 255, 383]).astype(np.int32)
#    io_binding.bind_cpu_input("query", query2)
#    io_binding.bind_cpu_input("key", key2)
#    io_binding.bind_cpu_input("value", value2)
#    io_binding.bind_cpu_input("slot_mappings", slot)
#    context_lens_ort.update_inplace(np.array([1,1,1]).astype(np.int32))
#    is_prompt_ort.update_inplace(np.array([0]).astype(np.int32))
#    sess.run_with_iobinding(io_binding)

class TestPagedAttentionOp(unittest.TestCase):

    def kv_cache_populated_correctly(self, key, value, key_cache, value_cache, slot_mappings, paged_kv_block_size, num_kv_heads, head_size):
        for token_idx in range(key.shape[0]):
            slot_idx = slot_mappings[token_idx]
            block_idx = slot_idx // paged_kv_block_size
            block_offset = slot_idx % paged_kv_block_size
            for embed_idx in range(key.shape[1]):
                self.assertTrue(np.abs(key[token_idx][embed_idx] - key_cache[block_idx][block_offset * num_kv_heads * head_size + embed_idx]) <= 0.001, 
                                f'key mismatches key cache at token_idx:{token_idx}, embed_idx:{embed_idx}, block_idx:{block_idx}, block_offset:{block_offset}')
                self.assertTrue(np.abs(value[token_idx][embed_idx] - value_cache[block_idx][block_offset * num_kv_heads * head_size + embed_idx]) <= 0.001,
                                f'value mismatches value cache at token_idx:{token_idx}, embed_idx:{embed_idx}, block_idx:{block_idx}, block_offset:{block_offset}')

    def test_cuda_paged_attention_prompt_only(self):
        sess = create_ort_session('/home/leca/code/onnxruntime-genai/test/custom_ops/build/libgenai_custom_ops_test.so', 
                                  batch_size=3, slot_cnt_per_block=16, block_cnt_per_layer=32, block_cnt_needed_by_longest_seq=8)
    
        query = np.random.randn(381,512).astype(np.float16) # 381 is the token num of all the sequences (127, 127, 127)
        key = np.random.randn(381,512).astype(np.float16)
        value = np.random.randn(381,512).astype(np.float16)
        key_cache = np.zeros([32,8192]).astype(np.float16)
        value_cache = np.zeros([32,8192]).astype(np.float16)
        block_tables = np.array([[0,1,2,3,4,5,6,7],[8,9,10,11,12,13,14,15],[16,17,18,19,20,21,22,23]]).astype(np.int32) # each sequence occupies 8 blocks (127/16)
        slot1 = np.arange(0, 127, dtype=np.int32)
        slot2 = np.arange(128, 255, dtype=np.int32)
        slot3 = np.arange(256, 383, dtype=np.int32)
        slot_mappings = np.concatenate((slot1, slot2, slot3))
        context_lens = np.array([127, 127, 127]).astype(np.int32)
        is_prompt = np.array([1]).astype(np.int32)
        y = sess.run(None, {'query':query, 'key':key, 'value':value, 'key_cache':key_cache, 'value_cache':value_cache, 'block_tables':block_tables, 'slot_mappings':slot_mappings, 'context_lens':context_lens, 'is_prompt':is_prompt})
        q_pt = torch.from_numpy(query.reshape(3, 127, 32, 16))
        k_pt = torch.from_numpy(key.reshape(3, 127, 32, 16))
        v_pt = torch.from_numpy(value.reshape(3, 127, 32, 16))
        out, _ = attention_ref(q_pt, k_pt, v_pt, causal=True, window_size=[-1, 0])
        y_np = np.array(y).reshape(381, 512)
        out_np = out.reshape(381, 512).numpy()
        self.assertTrue(np.allclose(y_np, out_np, rtol=1e-3, atol=1e-3, equal_nan=True))

    def test_cuda_paged_attention_decoding_only(self):
        seqlen_k, batch_size, nheads, d, paged_kv_block_size = 127, 2, 6, 16, 256
        sess = create_ort_session('/home/leca/code/onnxruntime-genai/test/custom_ops/build/libgenai_custom_ops_test.so',
                                  batch_size=batch_size, slot_cnt_per_block=paged_kv_block_size, block_cnt_per_layer=6, block_cnt_needed_by_longest_seq=3, num_heads=nheads, num_kv_heads=nheads, head_size=d)
        
        query = np.random.randn(batch_size, nheads*d).astype(np.float16)
        key = np.random.randn(batch_size, nheads*d).astype(np.float16)
        value = np.random.randn(batch_size, nheads*d).astype(np.float16)
        key_cache_6x256x6x16 = np.random.randn(6, paged_kv_block_size, nheads, d).astype(np.float16)
        value_cache_6x256x6x16 = np.random.randn(6, paged_kv_block_size, nheads, d).astype(np.float16)
        key_cache = key_cache_6x256x6x16.reshape(6, paged_kv_block_size * nheads * d)
        value_cache = value_cache_6x256x6x16.reshape(6, paged_kv_block_size * nheads * d)
        block_tables = np.array([[2,4,1],[5,3,0]]).astype(np.int32)
        context_lens = np.array([83,65]).astype(np.int32)
        slot_mappings = np.array([250, 500]).astype(np.int32)
        is_prompt = np.array([0]).astype(np.int32)
        y = sess.run(None, {'query':query, 'key':key, 'value':value, 'key_cache':key_cache, 'value_cache':value_cache, 'block_tables':block_tables, 'slot_mappings':slot_mappings, 'context_lens':context_lens, 'is_prompt':is_prompt})
            
        cache_seqlens = torch.from_numpy(context_lens)
        block_tables_pt = torch.from_numpy(block_tables)
        key_cache_pt = torch.from_numpy(key_cache_6x256x6x16)
        value_cache_pt = torch.from_numpy(value_cache_6x256x6x16)
        k_cache_cpu = rearrange(key_cache_pt[block_tables_pt.flatten()], '(b nblocks) block_size ... -> b (nblocks block_size) ...', b = batch_size)[:, :seqlen_k]
        v_cache_cpu = rearrange(value_cache_pt[block_tables_pt.flatten()], '(b nblocks) block_size ... -> b (nblocks block_size) ...', b = batch_size)[:, :seqlen_k]
    
        q = torch.from_numpy(query.reshape(batch_size, 1, nheads, d))
        k = torch.from_numpy(key.reshape(batch_size, 1, nheads, d))
        v = torch.from_numpy(value.reshape(batch_size, 1, nheads, d))
    
        arange = rearrange(torch.arange(seqlen_k), 's->1 s')
        cache_seqlens_expand = rearrange(cache_seqlens, 'b->b 1')
        key_padding_mask = arange < cache_seqlens_expand + 1
        update_mask = torch.logical_and(cache_seqlens_expand <= arange, arange < cache_seqlens_expand + 1)
        k_cache_cpu[update_mask] = rearrange(k, 'b s ... -> (b s) ...')
        v_cache_cpu[update_mask] = rearrange(v, 'b s ... -> (b s) ...')
        out_ref, _ = attention_ref(q, k_cache_cpu, v_cache_cpu, None, key_padding_mask, 0.0, None, causal=True)
        y_np = np.array(y).reshape(batch_size, nheads * d)
        out_np = out_ref.reshape(batch_size, nheads * d).numpy()
        self.assertTrue(np.allclose(y_np, out_np, rtol=1e-3, atol=1e-3, equal_nan=True))

    def test_cuda_paged_attention_prompt_rotary_embedding(self):
        sess = create_ort_session('/home/leca/code/onnxruntime-genai/test/custom_ops/build/libgenai_custom_ops_test.so',
                                  batch_size=3, slot_cnt_per_block=16, block_cnt_per_layer=32, block_cnt_needed_by_longest_seq=8, cos_sin_cache=True)

        query = np.random.randn(381,512).astype(np.float16) # 381 is the token num of all the sequences (127, 127, 127)
        key = np.random.randn(381,512).astype(np.float16)
        value = np.random.randn(381,512).astype(np.float16)
        key_cache = np.zeros([32,8192]).astype(np.float16)
        value_cache = np.zeros([32,8192]).astype(np.float16)
        block_tables = np.array([[0,1,2,3,4,5,6,7],[8,9,10,11,12,13,14,15],[16,17,18,19,20,21,22,23]]).astype(np.int32) # each sequence occupies 8 blocks (127/16)
        slot1 = np.arange(0, 127, dtype=np.int32)
        slot2 = np.arange(128, 255, dtype=np.int32)
        slot3 = np.arange(256, 383, dtype=np.int32)
        slot_mappings = np.concatenate((slot1, slot2, slot3))
        context_lens = np.array([127, 127, 127]).astype(np.int32)
        cos_sin_cache = np.random.uniform(-1, 1, size=[np.max(context_lens), 16]).astype(np.float16)   # uniform distribution in the range [-1, 1) for cos/sin value in the size of [max_seq_len, head_size]
        is_prompt = np.array([1]).astype(np.int32)
        y = sess.run(None, {'query':query, 'key':key, 'value':value, 'key_cache':key_cache, 'value_cache':value_cache, 'block_tables':block_tables, 'slot_mappings':slot_mappings, 'context_lens':context_lens, 'is_prompt':is_prompt, 'cos_sin_cache':cos_sin_cache})

    def test_cuda_paged_attention_decode_rotary_embedding(self):
        batch_size, nheads, d, paged_kv_block_size = 2, 6, 16, 256
        sess = create_ort_session('/home/leca/code/onnxruntime-genai/test/custom_ops/build/libgenai_custom_ops_test.so',
                                  batch_size=batch_size, slot_cnt_per_block=paged_kv_block_size, block_cnt_per_layer=6, block_cnt_needed_by_longest_seq=3, num_heads=nheads, num_kv_heads=nheads, head_size=d, cos_sin_cache=True)
    
        query = np.random.randn(batch_size, nheads*d).astype(np.float16)
        key = np.random.randn(batch_size, nheads*d).astype(np.float16)
        value = np.random.randn(batch_size, nheads*d).astype(np.float16)
        key_cache_6x256x6x16 = np.random.randn(6, paged_kv_block_size, nheads, d).astype(np.float16)
        value_cache_6x256x6x16 = np.random.randn(6, paged_kv_block_size, nheads, d).astype(np.float16)
        key_cache = key_cache_6x256x6x16.reshape(6, paged_kv_block_size * nheads * d)
        value_cache = value_cache_6x256x6x16.reshape(6, paged_kv_block_size * nheads * d)
        block_tables = np.array([[2,4,1],[5,3,0]]).astype(np.int32)
        context_lens = np.array([83,65]).astype(np.int32)
        slot_mappings = np.array([250, 500]).astype(np.int32)
        cos_sin_cache = np.random.uniform(-1, 1, size=[1, d]).astype(np.float16)
        is_prompt = np.array([0]).astype(np.int32)
        y = sess.run(None, {'query':query, 'key':key, 'value':value, 'key_cache':key_cache, 'value_cache':value_cache, 'block_tables':block_tables, 'slot_mappings':slot_mappings, 'context_lens':context_lens, 'is_prompt':is_prompt, 'cos_sin_cache':cos_sin_cache})

    def test_cuda_paged_attention_prompt_check_kvcache(self):
        sess = create_ort_session('/home/leca/code/onnxruntime-genai/test/custom_ops/build/libgenai_custom_ops_test.so',
                                  batch_size=3, slot_cnt_per_block=16, block_cnt_per_layer=32, block_cnt_needed_by_longest_seq=8)
        query = np.random.randn(381,512).astype(np.float16) # 381 is the token num of all the sequences (127, 127, 127)
        key = np.random.randn(381,512).astype(np.float16)
        value = np.random.randn(381,512).astype(np.float16)
        key_cache = np.zeros([32,8192]).astype(np.float16)
        value_cache = np.zeros([32,8192]).astype(np.float16)
        block_tables = np.array([[0,1,2,3,4,5,6,7],[8,9,10,11,12,13,14,15],[16,17,18,19,20,21,22,23]]).astype(np.int32) # each sequence occupies 8 blocks (127/16)
        slot1 = np.arange(0, 127, dtype=np.int32)
        slot2 = np.arange(128, 255, dtype=np.int32)
        slot3 = np.arange(256, 383, dtype=np.int32)
        slot_mappings = np.concatenate((slot1, slot2, slot3))
        context_lens = np.array([127, 127, 127]).astype(np.int32)
        is_prompt = np.array([1]).astype(np.int32)
    
        key_cache_ort = _ort.OrtValue.ortvalue_from_numpy(key_cache, "cuda")
        value_cache_ort = _ort.OrtValue.ortvalue_from_numpy(value_cache, "cuda")
    
        # prompt case
        io_binding = sess.io_binding()
        io_binding.bind_cpu_input("query", query)
        io_binding.bind_cpu_input("key", key)
        io_binding.bind_cpu_input("value", value)
        io_binding.bind_ortvalue_input("key_cache", key_cache_ort)
        io_binding.bind_ortvalue_input("value_cache", value_cache_ort)
        io_binding.bind_cpu_input("block_tables", block_tables)
        io_binding.bind_cpu_input("slot_mappings", slot_mappings)
        io_binding.bind_cpu_input("context_lens", context_lens)
        io_binding.bind_cpu_input("is_prompt", is_prompt)
        io_binding.bind_output("attn_out")
        sess.run_with_iobinding(io_binding)    
        self.kv_cache_populated_correctly(key, value, key_cache_ort.numpy(), value_cache_ort.numpy(), slot_mappings, paged_kv_block_size=16, num_kv_heads=32, head_size=16)

    def test_cuda_paged_attention_decoding_check_kvcache(self):
        batch_size, nheads, d, paged_kv_block_size = 2, 6, 16, 256
        sess = create_ort_session('/home/leca/code/onnxruntime-genai/test/custom_ops/build/libgenai_custom_ops_test.so',
                                  batch_size=batch_size, slot_cnt_per_block=paged_kv_block_size, block_cnt_per_layer=6, block_cnt_needed_by_longest_seq=3, num_heads=nheads, num_kv_heads=nheads, head_size=d)
        
        query = np.random.randn(batch_size, nheads*d).astype(np.float16)
        key = np.random.randn(batch_size, nheads*d).astype(np.float16)
        value = np.random.randn(batch_size, nheads*d).astype(np.float16)
        key_cache_6x256x6x16 = np.random.randn(6, paged_kv_block_size, nheads, d).astype(np.float16)
        value_cache_6x256x6x16 = np.random.randn(6, paged_kv_block_size, nheads, d).astype(np.float16)
        key_cache = key_cache_6x256x6x16.reshape(6, paged_kv_block_size * nheads * d)
        value_cache = value_cache_6x256x6x16.reshape(6, paged_kv_block_size * nheads * d)
        block_tables = np.array([[2,4,1],[5,3,0]]).astype(np.int32)
        context_lens = np.array([83,65]).astype(np.int32)
        slot_mappings = np.array([257, 250]).astype(np.int32)
        is_prompt = np.array([0]).astype(np.int32)
    
        key_cache_ort = _ort.OrtValue.ortvalue_from_numpy(key_cache, "cuda")
        value_cache_ort = _ort.OrtValue.ortvalue_from_numpy(value_cache, "cuda")
    
        io_binding = sess.io_binding()
        io_binding.bind_cpu_input("query", query)
        io_binding.bind_cpu_input("key", key)
        io_binding.bind_cpu_input("value", value)
        io_binding.bind_ortvalue_input("key_cache", key_cache_ort)
        io_binding.bind_ortvalue_input("value_cache", value_cache_ort)
        io_binding.bind_cpu_input("block_tables", block_tables)
        io_binding.bind_cpu_input("slot_mappings", slot_mappings)
        io_binding.bind_cpu_input("context_lens", context_lens)
        io_binding.bind_cpu_input("is_prompt", is_prompt)
        io_binding.bind_output("attn_out")
        sess.run_with_iobinding(io_binding)
        self.kv_cache_populated_correctly(key, value, key_cache_ort.numpy(), value_cache_ort.numpy(), slot_mappings, paged_kv_block_size, nheads, d)

if __name__ == "__main__":
    unittest.main()