# P8 本地运行与恢复手册

默认离线验收：`python3 -m unittest discover -s tests -v`。真实能力通过 README 所列环境变量显式启用；Skill 目录按 `narrative.md`、`outline.md`、`sample.md`、`deck.md`、`inspection.md` 分阶段加载，单文件上限 256 KiB。

模型调用超时返回 `gateway_error`；连接在请求发出后中断返回 `gateway_unknown_result`。两者都带诊断 ID 且不暴露密钥或供应商原始异常。未知结果不得自动重试：操作人员先查供应商请求记录，再通过任务摘要确认最后成功版本，最后由用户选择重试、修改输入或人工处理。

备份应复制整个数据目录并保留权限；恢复时先停止服务，将备份复制到新目录后以 `--data` 指向该目录启动。内核会重放 `pending-commit.json` 完成原子提交；交付目录和历史版本不可覆盖。恢复后运行测试，并抽查 `/summary`、版本列表、事件列表与交付 manifest hash。

日志和工单只记录诊断 ID、动作、耗时和结果类别；不得记录 API key、完整 Prompt、供应商原始响应或内部推理。
