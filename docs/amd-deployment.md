# AMD AUP Learning Cloud 部署

课程验证环境为 AMD Ryzen AI MAX+ PRO 395、Radeon 8060S（gfx1151）和 64GB 统一内存。使用 ROCm/HIP 编译 llama.cpp，并运行官方 Qwen3-8B Q4_K_M GGUF 模型。模型没有在本项目中训练或微调。

## 准备模型服务

在 AUP 平台选择 GPU Code Server，源码和模型保存在 `/home/jovyan` 持久目录。平台操作见 [官方说明](https://amdresearch.github.io/aup-learning-cloud/user-guide/code-server-guide.html)。

```bash
mkdir -p /home/jovyan/tapay-runtime/models
cd /home/jovyan/tapay-runtime
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
git checkout 0ac1a7fd35c127a18c0cbc6c57d12e256a6a385b
cmake -S . -B build -DGGML_HIP=ON \
  -DAMDGPU_TARGETS=gfx1151 -DCMAKE_HIP_ARCHITECTURES=gfx1151 \
  -DLLAMA_CURL=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build -j 4 --target llama-server
```

该提交对应课程已运行版本。其他 GPU 应使用实际架构目标，不能照抄 gfx1151。构建前确认平台的 ROCm/HIP 编译工具链可用。

从 [官方 Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF) 获取 `Qwen3-8B-Q4_K_M.gguf`，放入 `/home/jovyan/tapay-runtime/models/`。本仓库不代为下载或分发权重。

## 启动应用

在 `/home/jovyan/ROCm-AgentShop` 完成 README 的 Python 依赖与前端构建步骤，然后运行：

```bash
bash scripts/start-cloud.sh
```

启动脚本使用共享常驻模型服务：上下文 32768、单模型槽、关闭 thinking、启用 Jinja 工具模板。应用每次任务独立 worker，队列顺序消费。它不会为了每个角色重新加载模型。

可通过 `TAPAY_RUNTIME_DIR`、`TAPAY_LLAMA_SERVER`、`TAPAY_MODEL_FILE` 覆盖默认文件路径。模型端口固定 8081，应用端口 8000；已有端口服务不会被脚本杀掉或覆盖。

通过平台认证代理访问 `/user/你的平台账号/proxy/8000/`，不要提交真实凭据到代码仓库。默认只监听本机地址，应用不自带完整的多用户身份系统。

## 停止和恢复

实例到期会退出服务进程；重要文件应保存在平台持久目录，并自行备份。恢复实例后重新执行启动脚本。浏览器 SSE 断线可按事件编号续读；后端进程重启会把未完成任务标记为中断，保留历史记录，尚不支持任意模型步骤断点续跑。

平台配额、空闲硬件和服务实际在线状态需在每次演示前检查。脚本不自动开启新实例或延长配额。
