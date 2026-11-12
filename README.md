# 空值与边界下的湖文件保守裁剪器

本项目保存虚构算力设施的仓库源表、湖文件与Python读取工具。现有工具仅加载输入并显示记录数，不执行事实加工、目录提交、汇总或联接。所有设备、组织和金额均为合成。

## 字段与单位

catalog中的统计覆盖对应JSONL原文件，可信标记false的统计不可用于跳过。null为JSON null，类型按字段id登记。谓词是AST，不执行任意Python表达式。

material-index.json记录全部输入内容哈希；fixtures保存原始资料与配置，计算输出可放.workspace-output，原始资料不应被覆盖。

## 运行已有读取器

Python 3.11以上，标准库即可，不需安装第三方依赖。

    python -m src.pruning_reader fixtures/catalog.json
    python -m compileall -q src tests
    python -m unittest discover -s tests -v

## 固定容器

    docker build -t sound-scan-pruning:local .
    docker run --rm sound-scan-pruning:local

容器镜像以完整digest固定，默认仅检查输入契约；实现新增分析功能所需依赖由开发者明确登记。
