# Skill 渐进披露

> 分析状态：已验证  
> 优先级：P2  
> 模块类型：Extension  
> 主要代码：`src/mini_openharness/skills.py`；`cli.py:98-103,148-149`

## 1. 模块职责与独立性

**已确认**：SkillCatalog 发现 `<root>/<name>/SKILL.md`，启动时只把 name/description 注入 system prompt；完整正文只有模型调用 `load_skill` 后才作为 ToolResult 进入 history；`skills.py:20-87`。

它是独立目录契约和用户可见扩展点，但规模较小，因此 P2 短文。它不是通用 plugin system：不会动态 import Python 代码，也没有依赖/版本/生命周期管理。

## 2. 对外接口

- `Skill(name,description,path)`。
- `SkillCatalog(root).list/read/path/prompt()`；包根导出 Catalog。
- `LoadSkillTool(catalog)`：只读 Tool，name 固定为 `load_skill`。

## 3. 内部实现与执行流

Catalog 构造时扫描一层子目录的 `SKILL.md`，读取简化 frontmatter，只接受字母数字、下划线、连字符 name；重复 name 以后扫描项覆盖先前项；`skills.py:27-38,90-101`。

CLI 无 Skill 时不注册 load tool；有 Skill 时把 metadata prompt 加入 system prompt并注册工具。模型选择 load 后，Registry 仍执行 schema、permission、resource lock、timeout、Trace；成功时 Engine 额外发 `skill_loaded` trace。

## 4. 资源与边界

已知 Skill 锁定具体 SKILL.md file read；未知 name 锁 catalog root tree read，然后 `catalog.read()` 返回 ValueError，由 Registry 转为 error observation。

Skill 文件属于 workspace/指定目录中的可信指令内容，会影响模型行为；它不在代码 sandbox 中执行，但可能诱导模型调用有副作用工具，最终仍受 Permission/Hook 控制。

## 5. 扩展方式

新增 `<skills-dir>/<name>/SKILL.md` 并提供可识别 frontmatter。当前只解析单行 name/description，不支持完整 YAML、多行值、嵌套资源声明或 skill scripts。

## 6. 错误与边界情况

- Catalog 构造时会读取每个完整文件来提取 metadata，严格说不是 I/O 层面的“正文未加载”，只是正文未注入模型。
- frontmatter parser 是自定义子集，不支持 YAML 转义/多行。
- Skill name 冲突静默覆盖。
- Engine 通过精确名字 `load_skill` 识别 trace source，存在命名耦合。

## 7. 测试依据与设计评价

- `tests/test_skills.py::test_skill_metadata_is_discovered_before_body_is_loaded`
- `test_unknown_skill_is_rejected_by_registry`

值得学习的是用已有 Tool boundary 实现渐进披露，无需增加特殊执行通道。若规模增长，建议采用正式 frontmatter parser、冲突错误、metadata 缓存和结构化 Tool source。

## 8. 阅读建议

精读 `SkillCatalog._discover/prompt/read`、`LoadSkillTool.run/resources`、`_frontmatter`，再对照 CLI 注册与 Engine 的 `skill_loaded` 事件。
