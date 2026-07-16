    # 第 14 章：理解 Skill 的渐进式加载

    > 适用对象：刚开始学习 Python、第一次接触 Agent（智能体）运行框架的读者。  
    > 对应项目：`mini-openharness` 0.6.0。  
    > 建议做法：一边打开源码，一边按本章给出的行号和问题阅读。

    ## 1. 本章目标

阅读：

```text
src/mini_openharness/skills.py
skills/repository-guide/SKILL.md
tests/test_skills.py
```

## 2. Skill 在本项目中是什么

Skill 是一份可按需加载的说明文档，路径约定：

```text
<skills-root>/<skill-name>/SKILL.md
```

它不是可直接执行的 Python 插件。它更像一个“专业操作手册”，模型先知道有哪些手册，需要时再调用 `load_skill` 读取完整内容。

## 3. 为什么不把所有 Skill 全塞进 System Prompt

如果有几十个 Skill，每个数千字，全部加载会：

- 浪费上下文和 token；
- 干扰模型注意力；
- 增加无关指令冲突。

项目采用 progressive disclosure：

```text
开始时只展示 name + description
模型需要时调用 load_skill
才把完整 SKILL.md 加入工具结果
```

## 4. SkillCatalog 的发现过程

```python
for path in root.glob("*/SKILL.md"):
    content = path.read_text(...)
    metadata = _frontmatter(content)
```

只扫描一层子目录，结构简单可预测。

Frontmatter 示例：

```markdown
---
name: repository-guide
description: Guide for inspecting a repository
---
```

解析器只接受 `name` 和 `description`，不是完整 YAML 解析器。

## 5. 名称安全限制

```python
re.fullmatch(r"[A-Za-z0-9_-]+", name)
```

Skill 名只允许字母、数字、下划线和连字符。这样可避免奇怪路径片段或协议名称。

## 6. prompt() 只暴露目录

生成类似：

```text
Available skills are listed below. Call load_skill...
- repository-guide: ...
```

它被 CLI 拼入 System Prompt。模型知道 Skill 存在，但还看不到正文。

## 7. LoadSkillTool

它本身是一个只读工具：

```text
name = load_skill
arguments = {"name": "repository-guide"}
```

执行时调用 `catalog.read()` 返回完整内容。因为进入统一 ToolRegistry，它同样受到：

- Schema 校验；
- 资源锁；
- Hook；
- Trace；
- 工具错误回填。

## 8. 资源声明

已知 Skill 时锁定具体 `SKILL.md` 文件读资源；未知名称时保守锁定整个 Skills 根目录树。

即使 Skill 只是文本，也没有绕过统一的 effect-aware 调度模型。

## 9. Skill 与 Tool 的区别

```text
Skill：告诉模型“怎样做”，主要是知识和流程
Tool：赋予模型“能做什么”，实际产生读取、写入或外部调用
```

Skill 可以指导模型调用已有工具，但自己不等同于系统权限。

## 10. 本章练习

1. 为什么 Skill 只先暴露 metadata？
2. `load_skill` 为什么设计成 Tool 而不是 CLI 直接读取？
3. Skill 能否绕过 `write_file` 的权限？

## 11. 参考答案

1. 节省上下文并减少无关指令干扰。
2. 这样按需加载行为进入统一执行、Trace 和错误处理链。
3. 不能。Skill 只是文本说明，真正写文件仍需调用受控工具。
