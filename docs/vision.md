# 视觉识别环境

JournalFlow 使用完整 PaddleOCR-VL 1.6 流水线：PP-DocLayoutV3 检测版面，视觉语言模型识别区域，再重建 Markdown。不是把整页直接交给一个普通聊天模型，也不会把原生文字提取伪装成视觉识别。

## 两个环境

建议模型服务和客户端使用两个独立 Python 环境。模型服务需要兼容 NVIDIA GPU/驱动的 Linux 环境；客户端可把版面检测放在 CPU，识别请求发往模型服务。CPU 版面客户端与 GPU 版面客户端均采用同一视觉区域识别模型。

### 1. 模型服务

以下固定版本是项目使用的兼容组合。`vllm==0.21.0` 的默认轮子所带 CUDA/PyTorch 需要相应驱动；确认驱动兼容后再安装。其他驱动组合应按 vLLM 官方安装说明选择轮子或容器，而不是混装客户端依赖。

```bash
python3.12 -m venv .venv-model
source .venv-model/bin/activate
python -m pip install 'vllm==0.21.0'
MODEL_PATH=PaddlePaddle/PaddleOCR-VL-1.6 bash scripts/serve-model.sh
```

`MODEL_PATH` 也可指向已下载的官方模型目录。模型由上游提供，仓库不包含权重。下载受网络、平台和上游许可影响；使用上游正式下载渠道。
可参考 [Hugging Face 模型页](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) 或 [Paddle 官方 ModelScope 模型页](https://modelscope.cn/models/PaddlePaddle/PaddleOCR-VL-1.6)，下载后把 `MODEL_PATH` 设为本地目录。

服务默认只监听 `127.0.0.1:8118`，模型别名为 `PaddleOCR-VL-1.6-0.9B`。示例使用 BF16、16,384 上下文、32 并发序列和 0.68 显存占比。不同显存容量需要自行调节，不承诺任意 GPU 都能运行这些参数。

### 2. 客户端

在另一个 Linux Python 3.12 环境中：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[vision]'
python -m pip install 'paddlepaddle==3.2.1'
export PADDLE_PDX_MODEL_SOURCE=BOS
journalflow doctor --vision
```

默认 `layout_device="cpu"`。如果使用 GPU 做版面检测，请在这个客户端环境按 Paddle 官方 CUDA 安装说明安装匹配的 `paddlepaddle-gpu`，并修改配置；不要同时安装 CPU 和 GPU 两个 Paddle 包。

首次运行会下载 PP-DocLayoutV3。缓存位置可用 Paddle 官方的 `PADDLE_PDX_CACHE_HOME` 配置。

客户端本机没有兼容模型服务时，可以用 SSH 隧道连接自己的服务器：

```bash
ssh -N -L 8118:127.0.0.1:8118 user@gpu-host
```

保持隧道连接，并保留 `base_url="http://127.0.0.1:8118/v1"`。项目不保存 SSH 密码，不复制浏览器 Cookie。

## 配置与精校

`journalflow.toml` 的 `[vision]` 包含模型地址、别名、版面设备、批量页数、并发、DPI 和图像预算。服务必须兼容 PaddleOCR-VL 1.6 的识别协议；不是任意 OpenAI 风格多模态模型都能替换。

- 初次：300 DPI，最多 2,007,040 像素，8,192 生成令牌。
- 二次：450 DPI，最多 4,014,080 像素；只处理触发筛查的页。
- 触发条件包括低词语覆盖、表格数字覆盖不足、异常长输出、密集表格、复杂公式以及缺少可比较的原生文字。
- 二次结果增加质检问题、明显减少内容，或使覆盖指标变差时保留初次结果；删除异常重复允许有限的覆盖波动。
- 结果相近也不代表逐字正确，未解决的检查项会一直留在 `status.json`。

原图表保存为图片，表格保留结构，公式保留 LaTeX 和编号。正文、公式、参考文献和脚注的准确性仍取决于模型和版式。

## 恢复

重复运行 `convert` 会跳过同一 PDF 哈希、同一识别策略下已经检查通过的页。中断的批次可能需要重新识别。改变识别 DPI、模型或图像预算会生成新的策略目录，旧目录保留。

可先用自己的一篇授权 PDF 验证：

```bash
journalflow import-pdf ./example.pdf --doi 10.1234/example --journal IJOC --title "Example"
journalflow convert --limit 1
journalflow status
```

Windows 核心 CLI 会在 CI 验证；可选 Paddle 客户端和 GPU 模型服务的安装支持取决于上游平台轮子。推荐 Linux 客户端环境，Windows 可通过 WSL 使用。
