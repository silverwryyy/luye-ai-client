# -*- coding: utf-8 -*-
"""图片 shape 顺序 & cat(dim=?) 在图像张量上的含义。跑一次出结论，没装 torch 也能跑。"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import numpy as np
from PIL import Image, ImageDraw
import PIL

HERE = os.path.dirname(os.path.abspath(__file__))
PNG = os.path.join(HERE, "_demo_shape.png")

HAS_TORCH = False
try:
    import torch
    import torchvision
    HAS_TORCH = True
except Exception:
    pass

print("=" * 66)
print("环境")
print("  Python :", sys.version.split()[0])
print("  numpy  :", np.__version__)
print("  Pillow :", PIL.__version__)
print("  torch  :", (torch.__version__ + " / torchvision " + torchvision.__version__) if HAS_TORCH else "未安装 -> numpy 等价演示")

# ---------- 造一张不是正方形的图：宽 60，高 100 ----------
W, H = 60, 100
img = Image.new("RGB", (W, H), (28, 32, 44))
d = ImageDraw.Draw(img)
d.rectangle([0, 0, W - 1, H - 1], outline=(255, 90, 90), width=3)
d.line([0, 0, W - 1, H - 1], fill=(90, 200, 255), width=2)
img.save(PNG)

print("\n" + "=" * 66)
print("[1] PIL")
print("    img.size =", img.size, "  <- 注意是 (宽, 高)")
print("    真实值：宽 W =", W, " 高 H =", H)

arr = np.array(img)
print("\n[2] numpy")
print("    np.array(img).shape =", arr.shape, "  <- (高, 宽, 通道)，高在第 0 位")

if HAS_TORCH:
    from torchvision.transforms import ToTensor
    C = ToTensor()(img)
    kind = "(torch.Tensor)"
else:
    C = np.transpose(arr, (2, 0, 1)).astype(np.float32) / 255.0
    kind = "(numpy 模拟)"
print("\n[3] 转图像张量 (C, H, W)", kind)
print("    shape =", tuple(C.shape), "  <- 通道挪到最前，高 H 是第 1 位，宽 W 是第 2 位")


def shape_of(t):
    return tuple(t.shape)


def cat(a, b, dim):
    if HAS_TORCH:
        return tuple(torch.cat((a, b), dim=dim).shape)
    return tuple(np.concatenate((a, b), axis=dim).shape)


print("\n" + "=" * 66)
print("[4] (C,H,W) 自己和自己在各 dim 上拼接")
for dim, name in ((0, "通道 C"), (1, "高 H"), (2, "宽 W")):
    print("    dim=%d -> %-6s 结果 shape = %-16s (%s 翻倍)" % (dim, name, str(cat(C, C, dim)), name))
print("    dim=-1 等价于 dim=2 ->", cat(C, C, -1))

if HAS_TORCH:
    N = C.unsqueeze(0)
else:
    N = C[np.newaxis]
print("\n[5] 多加一维变 (N,C,H,W)，这是 PyTorch 最常见的图像张量")
print("    加 batch 维后 shape =", shape_of(N))
for dim, name in ((0, "batch 图片数"), (1, "通道"), (2, "高"), (3, "宽")):
    print("    dim=%d -> %-12s 结果 = %s" % (dim, name, str(cat(N, N, dim))))

print("\n" + "=" * 66)
print("[6] 形状不匹配会怎样：拿 (C,H,W) 与 (C,H,宽更小) 拼")
small = C[:, :, :30]
print("    x.shape =", shape_of(C), "  y.shape =", shape_of(small))
print("    dim=2 可以（只有宽不同，拼接轴正是它）->", cat(C, small, 2))
try:
    cat(C, small, 1)
except Exception as e:
    msg = str(e).replace("\n", " ")
    print("    dim=1 报错（高要相加，但宽 60 vs 30 不等）->", type(e).__name__, ":", msg[:150])

print("\n[7] cat 与 stack 的区别")
if HAS_TORCH:
    print("    cat((C,C), 0).shape   =", tuple(torch.cat((C, C), 0).shape), "  维数不变")
    print("    stack((C,C), 0).shape =", tuple(torch.stack((C, C), 0).shape), "  维数 +1")
else:
    print("    cat  等价 np.concatenate ->", tuple(np.concatenate((C, C), 0).shape), "  维数不变")
    print("    stack 等价 np.stack     ->", tuple(np.stack((C, C), 0).shape), "  维数 +1")

print("\n" + "=" * 66)
print("结论")
print("  1) dim 是「第几根轴」，轴的含义由你自己约定，不是天生的宽或高")
print("  2) 只在图像张量里：(C,H,W) 的 dim=1 才是高、dim=2 才是宽")
print("  3) PIL.size=(宽,高) / numpy=(高,宽,通道) / torch=(通道,高,宽) —— 三套顺序不同")
print("  4) 除了 dim 那根轴，其余维必须完全相等，否则报错")
print("  5) cat 延长已有轴（维数不变），stack 新开一轴（维数+1）")
print("\n生成的示例图：", PNG)
