"""
Triton implementation by @jlebar: https://gist.github.com/jlebar/3435b2c00deea53258887ce37231e5e2
"""

import torch
import triton
import triton.language as tl

AUTOTUNE_CONFIGS = [
    triton.Config(
        {
            "BLOCK_SIZE_M": 16,
            "BLOCK_SIZE_N": 128,
            "BLOCK_SIZE_K": 256,
            "GROUP_SIZE_M": 32,
        },
        num_stages=4,
        num_warps=4,
    ),
    triton.Config(
        {
            "BLOCK_SIZE_M": 128,
            "BLOCK_SIZE_N": 256,
            "BLOCK_SIZE_K": 128,
            "GROUP_SIZE_M": 32,
        },
        num_stages=4,
        num_warps=8,
    ),
]

AUTOTUNE_CONFIGS = [
    triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 256, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=3,
                   num_warps=8)
]

@triton.autotune(configs=AUTOTUNE_CONFIGS, key=["M", "N", "K"])
@triton.jit
def matmul_kernel(
    # Pointers to matrices
    a_ptr,
    b_ptr,
    c_ptr,
    # Matrix dimensions.
    M,
    N,
    K,
    # The stride variables represent how much to increase the ptr by when moving by 1
    # element in a particular dimension. E.g. `stride_am` is how much to increase `a_ptr`
    # by to get the element one row down (A has M rows).
    #
    # We assume `b` is packed with 2 `int4` elements per K, i.e. it's a
    # (K//2)xNx(2xint4) matrix, represented in Triton as (K//2)xNxi8.  If K
    # is the minor dimension, then stride_bk should logically be 0.5.  But
    # we don't want a fractional stride!  So let the given stride be the
    # stride per 2xint4.
    stride_am,
    stride_ak,
    stride_bk,
    stride_bn,
    stride_cm,
    stride_cn,
    # Meta-parameters
    BLOCK_SIZE_M: tl.constexpr,
    BLOCK_SIZE_N: tl.constexpr,
    BLOCK_SIZE_K: tl.constexpr,
    GROUP_SIZE_M: tl.constexpr,
    USE_INT8_WEIGHT: tl.constexpr,
):
    """Kernel for computing the matmul C = A x B.
    A has shape (M, K), B has shape (K, N) and C has shape (M, N)
    """
    tl.device_assert(K % BLOCK_SIZE_K == 0)

    # -----------------------------------------------------------
    # Map program ids `pid` to the block of C it should compute.
    # This is done in a grouped ordering to promote L2 data reuse.
    # See above `L2 Cache Optimizations` section for details.
    pid = tl.program_id(axis=0)
    num_pid_m = tl.cdiv(M, BLOCK_SIZE_M)
    num_pid_n = tl.cdiv(N, BLOCK_SIZE_N)
    num_pid_in_group = GROUP_SIZE_M * num_pid_n
    group_id = pid // num_pid_in_group
    first_pid_m = group_id * GROUP_SIZE_M
    group_size_m = min(num_pid_m - first_pid_m, GROUP_SIZE_M)
    pid_m = first_pid_m + (pid % group_size_m)
    pid_n = (pid % num_pid_in_group) // group_size_m

    # ----------------------------------------------------------
    # Create pointers for the first blocks of A and B.
    # We will advance this pointer as we move in the K direction
    # and accumulate
    # `a_ptrs` is a block of [BLOCK_SIZE_M, BLOCK_SIZE_K] pointers
    # `b_ptrs` is a block of [BLOCK_SIZE_K // 2, BLOCK_SIZE_N] pointers
    # See above `Pointer Arithmetic` section for details
    offs_am = (pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)) % M
    offs_bn = (pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)) % N
    offs_ak = tl.arange(0, BLOCK_SIZE_K)
    offs_bk = tl.arange(0, BLOCK_SIZE_K)

    '''
    a_ptrs = a_ptr + (offs_am[:, None] * stride_am + offs_ak[None, :] * stride_ak)
    b_ptrs = b_ptr + (offs_bk[:, None] * stride_bk + offs_bn[None, :] * stride_bn)
    '''
    b_ptrs = b_ptr + (offs_bk[None, :] * stride_bk + offs_bn[:, None] * stride_bn)
    a_ptrs = a_ptr + (offs_am[None, :] * stride_am + offs_ak[:, None] * stride_ak)

    # -----------------------------------------------------------
    # Iterate to compute a block of the C matrix.
    # We accumulate into a `[BLOCK_SIZE_M, BLOCK_SIZE_N]` block
    # of fp32 values for higher accuracy.
    # `accumulator` will be converted back to fp16 after the loop.
    # accumulator = tl.zeros((BLOCK_SIZE_M, BLOCK_SIZE_N), dtype=tl.float32)
    accumulator = tl.zeros((BLOCK_SIZE_N, BLOCK_SIZE_M), dtype=tl.float32)
    for k in range(0, tl.cdiv(K, BLOCK_SIZE_K)):
        # a = tl.load(a_ptrs, mask=offs_ak[None, :] < K - k * BLOCK_SIZE_K, other=0.0)
        a = tl.load(a_ptrs, mask=offs_ak[:, None] < K - k * BLOCK_SIZE_K, other=0.0)
        b = tl.load(b_ptrs)
        '''
        tl.static_assert(b.dtype == tl.int8)

        # Unpack `b` into an fp16 matrix, taking care to sign-extend b_lo.  Use
        # _4_i8 because the literal "4" is considered an i32, which causes the
        # shift operands to be widened to i32.
        _4_i8 = tl.full((1,), 4, dtype=tl.int8)
        b_lo = (b << _4_i8) >> _4_i8
        b_hi = b >> _4_i8
        # Workaround: Convert before the join() so that Triton can load the data
        # after the join using ldmatrix.
        b_f16 = (
            tl.join(b_lo.to(tl.bfloat16), b_hi.to(tl.bfloat16))
            .permute(0, 2, 1)
            .reshape(BLOCK_SIZE_K, BLOCK_SIZE_N)
        )
        '''

        if USE_INT8_WEIGHT:
            b_f16 = b.to(tl.bfloat16)
        else:
            b_f16 = b

        # accumulator += tl.dot(a, b_f16)
        accumulator += tl.dot(b_f16, a)
        a_ptrs += BLOCK_SIZE_K * stride_ak
        b_ptrs += BLOCK_SIZE_K * stride_bk

    c = accumulator.to(tl.bfloat16)

    offs_cm = pid_m * BLOCK_SIZE_M + tl.arange(0, BLOCK_SIZE_M)
    offs_cn = pid_n * BLOCK_SIZE_N + tl.arange(0, BLOCK_SIZE_N)
    # c_ptrs = c_ptr + stride_cm * offs_cm[:, None] + stride_cn * offs_cn[None, :]
    c_ptrs = c_ptr + stride_cm * offs_cm[None, :] + stride_cn * offs_cn[:, None]
    # c_mask = (offs_cm[:, None] < M) & (offs_cn[None, :] < N)
    c_mask = (offs_cm[None, :] < M) & (offs_cn[:, None] < N)
    tl.store(c_ptrs, c, mask=c_mask)


def matmul(a, b, use_int16):
    # assert a.shape[1] == b.shape[0] * 2, "Incompatible dimensions"
    assert a.is_contiguous(), "Matrix A must be contiguous"
    M, K = a.shape
    _, N = b.shape

    c = torch.empty((M, N), device=a.device, dtype=torch.bfloat16)
    grid = lambda META: (
        triton.cdiv(M, META["BLOCK_SIZE_M"]) * triton.cdiv(N, META["BLOCK_SIZE_N"]),
    )
    # triton.Config({'BLOCK_SIZE_M': 128, 'BLOCK_SIZE_N': 256, 'BLOCK_SIZE_K': 64, 'GROUP_SIZE_M': 8}, num_stages=3,
    if use_int16:
        kernel = triton.compiler.compile("/home/dberard/local/scripts/triton/int8mm/int16.ttgir")
        ret = kernel[triton.cdiv(M, 128) * triton.cdiv(N, 256), 1, 1](
            a,
            b,
            c,
            M,
            N,
            K,
            a.stride(0),
            # a.stride(1),
            b.stride(0),
            # b.stride(1),
            c.stride(0),
            # c.stride(1),
        )
    else:
        kernel = matmul_kernel
        ret = kernel[grid](
            a,
            b,
            c,
            M,
            N,
            K,
            a.stride(0),
            a.stride(1),
            b.stride(0),
            b.stride(1),
            c.stride(0),
            c.stride(1),
            USE_INT8_WEIGHT=use_int16,
        )
    if not use_int16:
        for ir in ("ttir", "ttgir", "llir", "ptx"):
            with open(f"/home/dberard/local/scripts/triton/int8mm/{'int16' if use_int16 else 'bf16'}.{ir}", "w") as f:
                f.write(ret.asm[ir])
    return c


def pack_2xint4(t):
    # Packs a KxNxfp16 matrix into a (K//2)xNx(2xint4) matrix.
    t = t.to(torch.int8).reshape(t.shape[0] // 2, 2, t.shape[1]).permute(1, 0, 2)
    return (t[0] & 0xF) | (t[1] << 4)

if __name__ == "__main__":
    import triton.profiler as proton

    a = torch.randn(2**16, 8192, device='cuda', dtype=torch.bfloat16)
    b = torch.randint(-8, 7, (8192, 1280), device='cuda').to(torch.bfloat16)
    use_int16 = False
    session_id = proton.start(name="profile_name", context="python")
    for _ in range(2):
        matmul(a, b, use_int16)
    proton.finalize()
