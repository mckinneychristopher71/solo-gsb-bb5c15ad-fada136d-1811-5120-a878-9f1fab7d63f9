# 选择性 JSON 事实披露服务（Selective JSON Disclosure）

提交一份嵌套 JSON 文档，服务会把所有 **叶事实**（标量、`null`、空对象 `{}`、空数组 `[]`）
按 [RFC 6901](https://www.rfc-editor.org/rfc/rfc6901) 路径展开，用「长度前缀 + 路径 +
规范 JSON 值」计算每个叶哈希，按路径排序构建可复现的 Merkle 根。之后可以只披露其中任意
若干条事实，并附带 Merkle 包含证明；验证方**完全不需要原始文档**，仅凭「根 + 所选值 +
证明」即可校验，且任何对路径、值或兄弟哈希的篡改都会导致根不匹配，错误带有精确定位。

## 一条命令启动

```bash
docker compose up --build
```

启动时容器内会自动执行 `pip install -r requirements.txt`（**依赖随启动安装**），
随后在 `0.0.0.0:8000` 启动 uvicorn。

- API 地址与端口：<http://localhost:8000>（容器内固定监听 `8000`）
- 交互式文档（Swagger UI）：<http://localhost:8000/docs>
- 健康检查：<http://localhost:8000/healthz>

如需改用宿主机其他端口：

```bash
HOST_PORT=9000 docker compose up --build      # => http://localhost:9000
```

停止：`Ctrl+C` 或 `docker compose down`。

## 必要配置

所有配置通过环境变量提供，均为可选项：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_PORT`（仅 Compose） | `8000` | 宿主机发布端口，容器内始终是 `8000` |
| `APP_HOST` | `0.0.0.0` | uvicorn 监听地址 |
| `APP_PORT` | `8000` | uvicorn 监听端口 |
| `MAX_BODY_BYTES` | `10485760`（10 MiB） | 请求体大小上限，超限返回 `413` |
| `DEFAULT_PROOF_MODE` | `standard` | `/commit` 未显式传 `proofMode` 时的默认格式，可选 `standard` / `compact` |
| `SKIP_PIP_INSTALL` | `0` | 设为 `1` 可跳过启动时的依赖安装 |
| `REQUIREMENTS_FILE` | `/app/requirements.txt` | 启动时安装的依赖清单路径 |

可复制 `.env.example` 为 `.env` 后按需修改。

## 密码学构造（完整规范）

哈希算法：**SHA-256**（32 字节），输出均以小写十六进制（64 字符）传输。

### 1. 叶展开

- 标量（字符串、数字、布尔、`null`）是叶子；
- 空对象 `{}` 与空数组 `[]` 也是叶子；
- 非空对象/数组递归展开，不出现在叶子集合中；
- 每个叶子的路径是 RFC 6901 指针字符串（如 `/subject/age`、`/a/0`、
  根标量为 `""`；键中的 `~` 编码为 `~0`、`/` 编码为 `~1`）。

### 2. 规范 JSON 值

同一逻辑值必须有唯一字节表示，采用如下规范形式：

- UTF-8 编码、无多余空白；
- 对象成员按键名的 Unicode 码点序排列（递归）；
- 整数最短十进制；浮点使用最短往返表示（Python `repr`）；
  拒绝 `NaN` / `Infinity`（不是合法 JSON 值）；
- 字符串仅做必要转义（`"`、`\`、控制字符，及 `\b \f \n \r \t`）；
- 空容器的规范值就是 `{}` 或 `[]`。

### 3. 叶哈希（长度前缀编码）

```
leaf_digest = SHA256(
    0x00                                  # 叶子域分隔标签
 || u32be(len(pointer_utf8)) || pointer_utf8
 || u32be(len(canonical_value_utf8)) || canonical_value_utf8
)
```

长度均为 4 字节大端无符号整数。标签 `0x00`/`0x01` 做域分隔，
防止叶子与内部节点作为哈希原像互相混淆。

### 4. 可复现 Merkle 根

1. 全部叶哈希按**路径字符串排序**（Unicode 码点序）；
2. 自左向右两两配对：`parent = SHA256(0x01 || left || right)`；
3. 某层节点数为**奇数时复制末节点**再配对（如 3 个叶子 →
   `H(L0,L1)` 与 `H(L2,L2)`，再向上配对）；
4. 恰有一个叶子时，根即为该叶哈希本身（不存在配对层）；
5. 根只取决于文档内容，与披露哪些路径无关（同一文档两次提交根相同）。

### 5. 包含证明（标准格式）

每个被披露叶子附带从叶到根的兄弟节点序列，每步为
`{"side": "left"|"right", "sibling": "<hex32>"}`：

- `side="right"`：`h = H(h, sibling)`；
- `side="left"`：`h = H(sibling, h)`；
- 单叶树证明为空数组 `[]`。

验证时由「提交的路径 + 提交的值」**重新计算叶哈希**，再按证明逐层折叠，
与提交的根比较。因此：

- 篡改**值** → 叶哈希变 → 根不匹配；
- 篡改**路径** → 叶哈希变（路径在原像中）→ 根不匹配；
- 篡改/替换任意**兄弟哈希**或其方向 → 折叠结果变 → 根不匹配；
- 篡改**根** → 与重算结果不相等。

验证接口不接收也无法接触原始文档。

### 6. 紧凑多叶证明（`proofMode: "compact"`）

多路径披露时，标准格式会在各叶的 `proof` 里**重复携带相同的上层兄弟节点**
（相邻叶子还会把彼此当作 level 0 的兄弟）。紧凑格式把每个非披露树节点
按其树坐标**全局唯一化**：

- `/commit` 请求加 `"proofMode": "compact"`；
- 响应顶层给出 `leafCount`（建树时的叶子总数，验证方据此知道每层宽度）；
- 每个被披露叶只有 `{"path", "index", "value"}`，`index` 是该叶在
  **按路径排序后的叶集合中的下标**（即建树下标）；
- 兄弟节点集中放在顶层 `proofNodes`，每项
  `{"level": <int>, "index": <int>, "hash": "<hex32>"}`，
  以 `(level, index)` 唯一标识并按 level、再按 index 排序；
  **同一节点只出现一次**，不再为各叶重复；
- `level` 从叶层起算为 0；根所在的最后一层不会出现在 `proofNodes` 中；
- 奇数层末节点与自身配对（`H(L,L)`），该节点**不需要**也**不允许**
  出现在 `proofNodes`；
- 披露全部叶子时 `proofNodes` 为空数组；单叶文档同样为空。

验证方仅凭「根 + 披露值 + 紧凑证明」**自底向上**重建根：每层已知节点
（披露叶、proof 节点、或由两个已知子节点折出的父节点）两两配对，
奇数层复制末节点，逐层向上直到根。结构校验失败返回 `400 BAD_PROOF` 并带
精确 `loc`：

| 结构错误 | `loc` 示例 |
| --- | --- |
| 叶下标越界 / 重复 | `leaves[1].index` |
| 节点层级越界（含给出根层） | `proofNodes[2].level` |
| 节点层内下标越界 | `proofNodes[2].index` |
| 奇数层末节点被携带 | `proofNodes[2]` |
| 节点与披露叶重合 | `proofNodes[2]` |
| `(level,index)` 重复 | `proofNodes[2]` |
| 缺少必需节点 | `proofNodes`（`detail.missing` 列出全部缺失项） |
| 携带多余节点 | `proofNodes[2]`（`detail.extraneous` 列出全部多余项） |
| 哈希不是 64 位十六进制 | `proofNodes[0].hash` |

结构合法但折叠出的根 ≠ 提交根（篡改路径、值或节点哈希）仍返回
`422 ROOT_MISMATCH`。`/verify` 根据请求体**自动识别**两种格式：
含 `proofNodes`/`leafCount` 即为紧凑格式，否则按标准格式逐叶验证。

### 7. 叶级差异（`POST /diff`）

`/diff` 对 `before`、`after` 两份文档分别执行同一套叶展开：标量、`null`、
空对象 `{}`、空数组 `[]` 都是叶子，根标量/根空容器使用空指针 `""`。随后按
RFC 6901 路径字符串排序并对齐两侧叶子：

- 仅 `after` 有 → `added`；
- 仅 `before` 有 → `removed`；
- 两侧都有但**规范 JSON 字节不同** → `changed`（例如 `1` 与 `1.0`、
  `true` 与 `1` 均视为不同）；
- 每个条目包含 `path`、`beforeValue`、`afterValue`、
  `beforeFingerprint`、`afterFingerprint`；
- 缺失侧的值与指纹均为 JSON `null`；实际 JSON `null` 值不缺失，其指纹是
  规范值 `null` 的 SHA-256；
- 指纹为 `SHA256(canonical_value_bytes)` 的小写十六进制；
- `added`、`removed`、`changed` 三个数组分别按 `path` 稳定排序。

可选 `paths` 过滤器只接受两侧叶路径并集中的**精确叶路径**：路径在任一侧是叶即可
（用于报告 added/removed；即使另一侧同一路径被替换成非空容器也允许）；若两侧都不是
叶子，且在任一侧解析为非空容器，则返回 `PATH_NOT_LEAF`；两侧都不存在返回
`PATH_NOT_FOUND`；重复项返回 `DUPLICATE_PATH`（`loc` 指向后出现的
`paths[i]`）。省略 `paths` 比较所有叶；显式传 `[]` 表示不选择任何路径。

### 8. RFC 6902 风格补丁（`POST /patch`）

`/patch` 接收一份 `document` 与按数组顺序排列的 `operations`，在文档的**深拷贝**上逐项
执行 [RFC 6902](https://www.rfc-editor.org/rfc/rfc6902) 风格操作，支持三种 `op`：

| `op` | 对象成员 | 数组元素 |
| --- | --- | --- |
| `add` | 成员不存在则新增，**已存在则替换其值** | 在指定下标前插入；`-` 或下标 `length` 表示末尾追加 |
| `remove` | 删除已存在的成员 | 删除已存在的元素，后续元素前移 |
| `replace` | 覆盖**已存在**成员的值 | 覆盖**已存在**下标的元素 |

规则细节：

- `path` 是 RFC 6901 指针（`~0`/`~1` 转义规则相同）；数组下标必须是无前导零的十进制
  整数（`"0"` 合法，`"01"` 非法）；
- `-` 是 RFC 6902 的数组末尾记号，**仅允许用于 `add`**；`remove`/`replace` 使用 `-`
  或越界下标都返回 `PATH_NOT_FOUND`；
- `add`/`replace` 必须携带 `value`（可以是任意 JSON 值，含嵌套容器，会经过与其他接口
  相同的 JSON 值校验）；`remove` 不接受 `value`（多余字段会被忽略）；
- 空指针 `""` 表示整个文档：允许 `add`/`replace`（替换根值）；`remove` 根文档无意义，
  返回 `INVALID_REQUEST`；
- 操作严格按顺序执行——后一操作的路径是否合法，取决于前一操作执行后的文档状态；
- **先完整校验、再整体生效**：所有操作的结构（类型、`op`、指针语法、必填字段）先于
  任何修改完成校验；执行阶段任一项的路径、父容器或数组下标非法，整份请求失败，
  错误统一为 `{error: {code, message, loc, detail?}}`，其中 `loc` 精确定位到
  `operations[i]`（结构字段错误为 `operations[i].op` / `.path` / `.value`），
  响应不包含 `document`/`changedPaths`，输入文档不被修改。

成功响应包含补丁后的 `document` 与 `changedPaths`：后者按与 `/commit`、`/diff` 相同的
叶模型计算两侧叶路径的差异——被删除的叶、新增的叶、两侧都存在但**规范 JSON 值不同**
的叶（`1`→`1.0` 视为变化）——并按 RFC 6901 指针字符串排序。补丁在规范意义上没有改变
文档时（例如以相同值 `replace`），`changedPaths` 为空数组；`operations` 为空数组
时原样返回文档且 `changedPaths` 为 `[]`。

### 9. 确定性叶级脱敏（`POST /redact`）

`/redact` 接收一份 `document` 与按数组顺序排列的 `rules`，在文档的**深拷贝**上对每个
**叶路径**（与 `/commit` 相同的叶模型：标量、`null`、空对象 `{}`、空数组 `[]` 都是叶子，
因此空容器也可被处理）执行确定性脱敏。每条规则形如
`{"pattern": "<RFC 6901 片段>", "action": "mask"|"drop", "value"?: ...}`。

**pattern** 是 RFC 6901 指针片段（`~0`/`~1` 转义规则相同），其每个引用记号可以是：

- 字面量（如 `/subject/name`、`/a/0`）；
- `*`：恰好匹配一层记号（成员名或数组下标均可），不跨层，例如 `/emails/*` 匹配
  `/emails/0`、`/emails/1`，但不匹配 `/emails` 或 `/emails/0/x`；
- `**`：匹配**零层或任意多层**记号，例如 `/x/**/name` 同时匹配 `/x/name`（零层）、
  `/x/y/name` 与 `/x/y/z/name`；`/**` 匹配全部叶路径；
- 空片段 `""` 是字面量根指针，只匹配根标量/根空容器；
- 记号内不能把 `*` 与其他字符混用（如 `/a*` 非法，因为 `*` 无法被转义为字面量），
  非法模式统一返回 `POINTER_SYNTAX`，`loc` 指向 `rules[i]`。

**action**：

| action | 对象成员 | 数组元素 | 要求 |
| --- | --- | --- | --- |
| `mask` | 以常量 `value` 整体替换该叶值 | 同左 | 必须携带 `value`，可以是任意 JSON 值（含嵌套容器，经过与其他接口相同的值校验） |
| `drop` | 删除该成员 | 元素**置为 `null`**（不移动后续元素下标） | 不接受 `value`（多余字段会被忽略） |

根指针 `""` 可以 `mask`（整体替换文档），但不能 `drop`（文档本身没有父槽位），
命中时返回 `INVALID_REQUEST`，`loc` 指向对应规则。

**同一叶命中多条规则时的胜出顺序是确定性的**：

1. 字面量记号数更多的规则胜出（`*` 与 `**` 都算非字面量）；
2. 字面量数相同时，`*` 记号数更多者胜出（即 `*` 优先于 `**`）；
3. 仍相同时，按 `rules` 数组顺序，先出现者胜出。

所有规则的结构与模式语法**先完整校验、再整体生效**：任一条规则非法，整份请求失败，
响应不含 `document`/`applied`，输入文档不被修改。规则判定针对原始文档形状一次完成，
因此删除成员或置空数组元素不会影响其他叶的判定。

成功响应为 `{"document": ..., "applied": [...]}`；`applied` 为每个**实际被处理的叶**
给出一条 `{"path", "action", "ruleIndex", "pattern"}`（`ruleIndex` 是胜出规则的数组
下标、`pattern` 是其模式原文），并按 RFC 6901 指针字符串排序；未命中任何规则的叶
原样保留且不出现在 `applied` 中；`rules` 为空数组时原样返回文档、`applied` 为 `[]`。

请求：

```json
{
  "document": {
    "subject": {"name": "Ada", "age": 36, "ssn": "078-05-1120"},
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {}
  },
  "rules": [
    {"pattern": "/subject/ssn", "action": "mask", "value": "***"},
    {"pattern": "/emails/*", "action": "mask", "value": "redacted"},
    {"pattern": "/subject/age", "action": "drop"},
    {"pattern": "/**", "action": "mask", "value": "fallback"}
  ]
}
```

成功（`200`；前两条字面/单层规则优先于兜底的 `/**`，`/subject/name` 才落到 `/**`）：

```json
{
  "document": {
    "subject": {"name": "fallback", "ssn": "***"},
    "emails": ["redacted", "redacted"],
    "empty": {}
  },
  "applied": [
    {"path": "/emails/0", "action": "mask", "ruleIndex": 1, "pattern": "/emails/*"},
    {"path": "/emails/1", "action": "mask", "ruleIndex": 1, "pattern": "/emails/*"},
    {"path": "/subject/age", "action": "drop", "ruleIndex": 2, "pattern": "/subject/age"},
    {"path": "/subject/name", "action": "mask", "ruleIndex": 3, "pattern": "/**"},
    {"path": "/subject/ssn", "action": "mask", "ruleIndex": 0, "pattern": "/subject/ssn"}
  ]
}
```

### 10. 结构清单与同形哈希（`POST /shape`）

`/shape` 接收一份 `document`，返回**不含任何叶值**的结构清单与整树同形哈希
`shapeHash`：调用方无需上送两份原文，只需交换哈希即可比较两份 JSON 是否「同形」
（节点类型与对象键名完全一致，标量值不参与比较）。

- `nodes`：文档中**每个节点**（含根，根路径为 `""`）的 `{"path", "type"}`，按 RFC 6901
  指针字符串排序；`type` 为 JSON 节点类型（`object` / `array` / `string` / `number` /
  `boolean` / `null`），标量只出现类型、不出现值；
- `nodeCount`：清单条目数（含根节点）；
- `shapeHash`：自底向上计算的 SHA-256。每个节点的哈希原像以一字节**域标签**开头，
  容器再带 **u32be 子节点计数**与**长度前缀的键名**——类型与子节点边界都有明确
  分隔，不同结构的树不可能序列化出相同字节串（无拼接歧义）：

```
null        0x10
boolean     0x11                              （true/false 不参与）
number      0x12                              （值不参与）
string      0x13                              （值不参与）
空对象      0x14
空数组      0x15
object      0x16 || u32be(成员数)
              || ( u32be(键名长度) || 键名UTF-8 || 子节点哈希 )*   （键按码点排序）
array       0x17 || u32be(元素数) || 子节点哈希*                  （保留数组顺序）
```

规则细节：

- 空容器与标量类型各自使用独立标签（与 Merkle 叶模型一致：空 `{}` / `[]` 是叶子）；
- 对象键名参与哈希（键名是结构的一部分），成员先按 Unicode 码点排序再折叠，
  因此**键序不影响哈希**；数组元素**保留顺序**，`[1, "x"]` 与 `["x", 1]` 不同形；
- 标量值不进入任何原像：`36`→`41`、`"Ada"`→`"Bob"`、`1`→`1.0`、`true`→`false`
  哈希都不变；改类型（`36`→`"36"`）、改键名、增删成员、调换数组顺序都会改变哈希；
- 标签使用独立的 `0x1x` 区间，与 Merkle 叶（`0x00`）/ 内部节点（`0x01`）原像互不混淆；
- 两份文档同形 ⟺ `shapeHash` 相等。

请求：

```json
{
  "document": {
    "subject": {"name": "Ada", "age": 36},
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {}
  }
}
```

成功（`200`；清单只含路径与类型，不回显任何值）：

```json
{
  "shapeHash": "d215b72418b62a38f5c0977983908b4a775f87b00a913bf5dc07e2883a6612b5",
  "nodeCount": 8,
  "nodes": [
    {"path": "", "type": "object"},
    {"path": "/emails", "type": "array"},
    {"path": "/emails/0", "type": "string"},
    {"path": "/emails/1", "type": "string"},
    {"path": "/empty", "type": "object"},
    {"path": "/subject", "type": "object"},
    {"path": "/subject/age", "type": "number"},
    {"path": "/subject/name", "type": "string"}
  ]
}
```

把 `36` 改成 `41`、`"Ada"` 改成 `"Bob"` 后 `shapeHash` 不变；把 `36` 改成 `"36"`
（数字变字符串）或把 `"empty": {}` 改成 `[]`，`shapeHash` 即改变。

### 11. 三路合并（`POST /merge`）

`/merge` 接收 `base`、`left`、`right` 三份 JSON 文档——`left` 与 `right` 都是从共同版本
`base` 分出的编辑——按 **RFC 6901 路径**对**对象成员递归合并**（对象成员逐个对齐，数组不逐元素合并）：

- 某路径**仅一侧相对 base 改动**（修改、删除、新增皆可）→ 采用该侧结果；另一侧保持 base 值不会阻挡；
- 两侧都改了该路径，但改成**同一个规范 JSON 值**（对象成员顺序不同但规范字节相同也算）→ 采用该值；
- 两侧都新增同一键、值按规范相等 → 采用该值；
- 两侧对同一路径做了**不同改动** → 记为冲突，冲突类型包括：两侧改成不同值、
  **删除与修改并发**（一侧删成员、另一侧改值）、两侧新增不同值、子树被一侧整体替换而另一侧改其成员等；
- **数组整体视为一个值**：两侧对同一数组的任何不同改动（哪怕只是不同元素）都是 `/tags`
  这一条路径上的冲突，不做逐元素拼接；仅一侧改数组则正常合并；
- 相等性以**规范 JSON 字节**判定：成员缺失与值为 `null` 必须区分（一侧留 `null`、另一侧删成员
  属于删除/修改并发冲突），数值 `1` 与 `1.0` 不视为相同（`true` 与 `1` 亦然）；
- 仅当三侧在某路径**都是对象**时才向下递归成员；对象与标量互转、整棵子树删除都按整条路径的原子改动处理；
- 合并结果的成员顺序是确定性的：原 `base` 成员按 `base` 顺序保留，两侧新增的成员按键名码点序追加。

响应只有两种形态（HTTP 均为 `200`）：

- 无冲突：`{"merged": <合并文档>}`；
- 有一处或多处冲突：`{"conflicts": [...]}`，**不返回任何部分合并文档**（即使大量路径可无冲突合并）。
  `conflicts` 按 RFC 6901 指针字符串排序（转义后的形态，即 `~0` 排在 `~1` 前），每条给出三侧的
  **存在标记与值**：

```json
{
  "path": "/a/b",
  "base":  {"present": true,  "value": 1},
  "left":  {"present": true,  "value": 9},
  "right": {"present": false, "value": null}
}
```

`present:false` 表示该侧**没有这个成员**（此时 `value` 固定为 `null` 占位；请用 `present` 区分
「成员缺失」与「成员存在且值就是 JSON `null`」）。根路径冲突的 `path` 为空串 `""`。

三份文档都经过与其他接口相同的严格 JSON 解析（重复键返回 `INVALID_JSON`）和值校验
（`NaN`/`Infinity` 返回 `UNSUPPORTED_VALUE`，`loc` 指向 `base`/`left`/`right`）；
缺字段返回 `INVALID_REQUEST`，`loc` 指向对应字段。

## API

所有请求/响应均为 JSON。错误响应统一形如：

```json
{ "error": { "code": "ERROR_CODE", "message": "…", "loc": "leaves[0].proof[1].sibling" } }
```

`loc` 指向出错字段（含数组下标），用于定位篡改或非法输入；部分错误还带 `detail`。

### `POST /commit` — 提交并生成证明

请求：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 36, "isAdult": true, "nickname": null },
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {},
    "deep": { "a/b": { "tilde~key": [7] } }
  },
  "reveal": ["/subject/age", "/emails/1", "/empty", "/deep/a~1b/tilde~0key/0"]
}
```

响应（**仅含所选值及证明**，不会回显文档其他部分）：

```json
{
  "root": "9f0c…（64 位十六进制）",
  "leaves": [
    { "path": "/deep/a~1b/tilde~0key/0", "value": 7,
      "proof": [ {"side": "right", "sibling": "…"}, … ] },
    { "path": "/emails/1", "value": "ada@analytical.engine", "proof": [ … ] },
    { "path": "/empty", "value": {}, "proof": [ … ] },
    { "path": "/subject/age", "value": 36, "proof": [ … ] }
  ]
}
```

不带 `proofMode`（或显式 `"standard"`）时响应保持上述形状不变。

#### 紧凑模式（`"proofMode": "compact"`）

请求：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 36, "isAdult": true, "nickname": null },
    "emails": ["ada@example.com", "ada@analytical.engine"],
    "empty": {}
  },
  "reveal": ["/subject/age", "/emails/1", "/empty"],
  "proofMode": "compact"
}
```

响应（7 个叶子按路径排序；兄弟节点按 `(level,index)` 去重后只有 2 个，
标准格式需要 3×若干步）：

```json
{
  "root": "f066f677ef752e68f8924da8a10ba44b2beb5a4e3ff9793e20cd38c3a3120607",
  "proofMode": "compact",
  "leafCount": 7,
  "leaves": [
    { "path": "/emails/1", "index": 1, "value": "ada@analytical.engine" },
    { "path": "/empty", "index": 2, "value": {} },
    { "path": "/subject/age", "index": 3, "value": 36 }
  ],
  "proofNodes": [
    { "level": 0, "index": 0,
      "hash": "10854943caa6ff2053c8e3c9c9b183b632d3a8fd04cbe94a1f518bdb5d2ab6b9" },
    { "level": 2, "index": 1,
      "hash": "a3a2a836c671f329a313d7f9493639bc7689d2bc3a2892be6f14c3657a1223e0" }
  ]
}
```

紧凑证明直接原样发给 `/verify`（不需要 `proofMode` 字段，服务自动识别）。

### `POST /diff` — 叶级变更报告

请求（`paths` 可省略；省略时比较两侧全部叶子的并集）：

```json
{
  "before": {
    "name": "Ada",
    "age": 36,
    "role": null,
    "metadata": {},
    "removed": "x"
  },
  "after": {
    "name": "Ada",
    "age": 37,
    "role": "owner",
    "metadata": {},
    "added": []
  },
  "paths": ["/age", "/role", "/removed", "/added"]
}
```

成功（`200`）：

```json
{
  "added": [
    {
      "path": "/added",
      "beforeValue": null,
      "afterValue": [],
      "beforeFingerprint": null,
      "afterFingerprint": "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"
    }
  ],
  "removed": [
    {
      "path": "/removed",
      "beforeValue": "x",
      "afterValue": null,
      "beforeFingerprint": "ba2df4903a2c14e86dc3bcca58911b44ac1d2514b7227bf6eb08cfb978f55a1b",
      "afterFingerprint": null
    }
  ],
  "changed": [
    {
      "path": "/age",
      "beforeValue": 36,
      "afterValue": 37,
      "beforeFingerprint": "76a50887d8f1c2e9301755428990ad81479ee21c25b43215cf524541e0503269",
      "afterFingerprint": "7a61b53701befdae0eeeffaecc73f14e20b537bb0f8b91ad7c2936dc63562b25"
    },
    {
      "path": "/role",
      "beforeValue": null,
      "afterValue": "owner",
      "beforeFingerprint": "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b",
      "afterFingerprint": "622f06cbcd2e7e61637e17212e714e6c721d2dd5a9f7bc1aec911b65b34edcf5"
    }
  ]
}
```

未选择的路径不会出现在结果中。空文档、空容器和根标量按叶规则处理：
`{}` 与 `[]` 的根路径都是 `""`；空文档之间比较得到三个空数组。

过滤器错误沿用统一错误体，例如提交非叶容器路径：

```json
{ "error": {
    "code": "PATH_NOT_LEAF",
    "message": "path '/subject' is a non-empty container in 'before'; paths must be exact leaf paths",
    "loc": "paths[0]",
    "detail": { "path": "/subject", "leafChildren": ["/subject/age", "/subject/name"] }
} }
```

### `POST /patch` — 顺序执行 RFC 6902 补丁

请求（`operations` 按数组顺序执行；数组可用下标插入或用 `-` 追加）：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 36 },
    "emails": ["ada@example.com"],
    "role": null
  },
  "operations": [
    { "op": "replace", "path": "/subject/age", "value": 37 },
    { "op": "add", "path": "/emails/-", "value": "ada@analytical.engine" },
    { "op": "add", "path": "/subject/title", "value": "Countess" },
    { "op": "remove", "path": "/role" }
  ]
}
```

成功（`200`）：

```json
{
  "document": {
    "subject": { "name": "Ada", "age": 37, "title": "Countess" },
    "emails": ["ada@example.com", "ada@analytical.engine"]
  },
  "changedPaths": [
    "/emails/1",
    "/role",
    "/subject/age",
    "/subject/title"
  ]
}
```

非法路径/父容器/数组下标时，整份请求失败且不返回任何补丁结果，`loc` 指向出错的
操作（先做结构校验，再按顺序执行，任何一步失败都不会产生部分结果）：

```json
{ "error": {
    "code": "PATH_NOT_FOUND",
    "message": "array index 5 out of range for 'replace' (length 1)",
    "loc": "operations[2]",
    "detail": { "path": "/emails/5" }
} }
```

### `POST /verify` — 无原文校验

请求体只需提交方给你的根与所选披露：

```json
{
  "root": "9f0c…",
  "leaves": [
    { "path": "/subject/age", "value": 36,
      "proof": [ {"side": "left", "sibling": "…"} ] }
  ]
}
```

成功（`200`）：

```json
{ "valid": true, "root": "9f0c…",
  "leaves": [ {"path": "/subject/age", "value": 36} ] }
```

紧凑格式的 `/verify` 请求体即 `/commit` 紧凑响应去掉 `proofMode`
（保留 `root`、`leafCount`、`leaves`、`proofNodes`），成功时响应多一个
`"proofMode": "compact"`：

```json
{
  "root": "f066f677ef752e68f8924da8a10ba44b2beb5a4e3ff9793e20cd38c3a3120607",
  "leafCount": 7,
  "leaves": [
    { "path": "/emails/1", "index": 1, "value": "ada@analytical.engine" },
    { "path": "/empty", "index": 2, "value": {} },
    { "path": "/subject/age", "index": 3, "value": 36 }
  ],
  "proofNodes": [
    { "level": 0, "index": 0, "hash": "108549…b9" },
    { "level": 2, "index": 1, "hash": "a3a2a8…e0" }
  ]
}
```

紧凑证明的**结构错误**（缺节点、多节点、下标越界/重复、奇数层末节点等）
是 `400 BAD_PROOF`，例如漏掉一个必需节点：

```json
{ "error": {
    "code": "BAD_PROOF",
    "message": "compact proof is missing the required sibling node at level 0, index 3 …",
    "loc": "proofNodes",
    "detail": { "missing": [ {"level": 0, "index": 3} ] }
} }
```

任一条证明内容失败（`422`，其余结构性输入错误为 `400/413`）：

```json
{ "error": {
    "code": "ROOT_MISMATCH",
    "message": "proof for path '/subject/age' failed: recomputed root does not match …",
    "loc": "leaves[0]",
    "detail": { "path": "/subject/age", "committedRoot": "…",
                "hint": "the path, the value, a sibling hash or the root was tampered with" }
} }
```

### `POST /merge` — 三路合并两份分支编辑

请求（`base` 是共同祖先，`left`/`right` 是两路编辑）：

```json
{
  "base":  {"name": "Ada", "age": 36, "tags": ["a", "b"], "role": "analyst"},
  "left":  {"name": "Ada", "age": 37, "tags": ["a", "b"], "role": "analyst", "team": "engine"},
  "right": {"name": "Ada", "age": 36, "tags": ["a", "c"], "team": "engine"}
}
```

无冲突成功（`200`；`age` 采用 left 的单侧修改、`tags` 采用 right 的单侧修改、
两侧相同的 `team` 新增合并、right 对 `role` 的单侧删除生效——left 保留 base 值不阻挡删除）：

```json
{
  "merged": {"name": "Ada", "age": 37, "tags": ["a", "c"], "team": "engine"}
}
```

若两侧对同一路径做了不同改动（下例请求中 `role` 被 left 改成 `"owner"`、right 删除；
`tags` 两侧对同一数组做了不同改动；`age` 仅 left 改、`team` 两侧相同新增本可合并），
则返回**全部冲突**且不含 `merged`（不返回部分结果）：

```json
{
  "base":  {"name": "Ada", "age": 36, "tags": ["a", "b"], "role": "analyst"},
  "left":  {"name": "Ada", "age": 37, "tags": ["a", "b", "z"], "role": "owner", "team": "engine"},
  "right": {"name": "Ada", "age": 36, "tags": ["a", "c"], "team": "engine"}
}
```

```json
{
  "conflicts": [
    {
      "path": "/role",
      "base":  {"present": true,  "value": "analyst"},
      "left":  {"present": true,  "value": "owner"},
      "right": {"present": false, "value": null}
    },
    {
      "path": "/tags",
      "base":  {"present": true,  "value": ["a", "b"]},
      "left":  {"present": true,  "value": ["a", "b", "z"]},
      "right": {"present": true,  "value": ["a", "c"]}
    }
  ]
}
```

冲突条目按 RFC 6901 指针字符串排序；`present` 区分成员缺失与值为 `null`；
`1` 与 `1.0` 是不同值，同一数组的并发修改是一条整体冲突。

### 错误码一览

| HTTP | code | 触发场景（示例） |
| --- | --- | --- |
| 400 | `INVALID_JSON` | 请求体不是合法 JSON、含重复对象键 |
| 400 | `INVALID_REQUEST` | 缺字段、类型错误 |
| 400 | `POINTER_SYNTAX` | 指针非法（如尾随 `~`、不以 `/` 开头） |
| 400 | `PATH_NOT_FOUND` | 文档中不存在该路径（含数组越界、`remove`/`replace` 命中不存在的成员、对 `-` 做非 `add` 操作等） |
| 400 | `PATH_NOT_LEAF` | 路径是非空容器（`/commit` 的 `reveal` 或 `/diff` 的 `paths`）；`detail.leafChildren` 给出可选的叶路径 |
| 400 | `DUPLICATE_PATH` | 同一指针重复提交 |
| 400 | `EMPTY_REVEAL` | 披露列表为空 |
| 400 | `UNSUPPORTED_VALUE` | `NaN`/`Infinity`、孤立代理项等（含 `/patch` 的 `operations[i].value`） |
| 400 | `BAD_HASH_ENCODING` | 根或兄弟哈希不是 64 位十六进制 |
| 400 | `BAD_PROOF` | 证明结构错误：标准格式 side 非法、兄弟长度不对；紧凑格式叶/节点下标越界或重复、节点缺失或多余、携带奇数层末节点等（均有精确 `loc`） |
| 413 | `PAYLOAD_TOO_LARGE` | 请求体超过 `MAX_BODY_BYTES` |
| 422 | `ROOT_MISMATCH` | 证明有效但重算根 ≠ 提交根（路径/值/兄弟/节点哈希/根被篡改） |

### 其他

- `GET /healthz` → `{"status":"ok"}`
- `POST /diff` → 叶级 added/removed/changed 变更报告
- `POST /patch` → 顺序执行 RFC 6902 add/remove/replace 补丁，返回 patched 文档与 changedPaths
- `POST /redact` → 按 RFC 6901 片段（字面量/`*`/`**`）确定性 mask/drop 叶路径，返回 redacted 文档与 applied 列表
- `POST /shape` → 不含叶值的结构清单（路径+类型）与自底向上同形哈希 `shapeHash`
- `POST /merge` → base/left/right 三路合并：对象成员递归、数组整体取值、规范值相等、冲突按 RFC 6901 路径排序并给出三侧存在标记与值
- `GET /` → 服务与端点元信息
- `GET /docs` / `GET /openapi.json` → OpenAPI 文档

## 快速试用

```bash
# 1) 提交文档，只披露 /subject/age
curl -s localhost:8000/commit -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["a@x", "b@y"]},
  "reveal": ["/subject/age"]
}' | tee /tmp/commit.json

# 2) 用返回的 root + leaves 验证（不提交 document）
curl -s localhost:8000/verify -H 'content-type: application/json' \
  -d "{\"root\": $(jq .root /tmp/commit.json), \"leaves\": $(jq .leaves /tmp/commit.json)}"

# 3) 把值改成 37 再验证 → 422 ROOT_MISMATCH，loc=leaves[0]

# 4) 报告两份文档的叶级差异；省略 paths 时比较所有叶子
curl -s localhost:8000/diff -H 'content-type: application/json' -d '{
  "before": {"subject": {"name": "Ada", "age": 36}},
  "after": {"subject": {"name": "Ada", "age": 37, "added": null}},
  "paths": ["/subject/age", "/subject/added"]
}'

# 4b) 按顺序执行 RFC 6902 add/remove/replace 补丁；- 表示数组末尾
curl -s localhost:8000/patch -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["ada@x"]},
  "operations": [
    {"op": "replace", "path": "/subject/age", "value": 37},
    {"op": "add", "path": "/emails/-", "value": "ada@engine"},
    {"op": "remove", "path": "/subject/name"}
  ]
}'

# 5) 紧凑多叶证明：加 "proofMode": "compact"，返回 leafCount、
#    每个叶的 index，以及按 (level,index) 去重的 proofNodes
curl -s localhost:8000/commit -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["a@x", "b@y"]},
  "reveal": ["/subject/name", "/subject/age"],
  "proofMode": "compact"
}'
# /verify 自动识别紧凑格式：把紧凑响应的 root/leafCount/leaves/proofNodes
# 原样回传即可（无需携带 document 或 proofMode）。

# 6) 确定性脱敏：字面量优先于 *，* 优先于 **；drop 删除对象成员、数组元素置 null
curl -s localhost:8000/redact -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "ssn": "078-05-1120"}, "emails": ["a@x", "b@y"]},
  "rules": [
    {"pattern": "/subject/ssn", "action": "mask", "value": "***"},
    {"pattern": "/emails/*", "action": "mask", "value": "redacted"},
    {"pattern": "/**", "action": "mask", "value": null}
  ]
}'

# 7) 同形比较：两份文档分别取 shapeHash，相等即同形（标量值不参与）
curl -s localhost:8000/shape -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Ada", "age": 36}, "emails": ["a@x", "b@y"]}
}'
curl -s localhost:8000/shape -H 'content-type: application/json' -d '{
  "document": {"subject": {"name": "Bob", "age": 41}, "emails": ["c@z", "d@w"]}
}'
# 两次响应的 shapeHash 相同；把 41 改成 "41"（字符串）再请求，哈希即改变

# 8) 三路合并：仅一侧改动/两侧改成同一规范值即合并；数组整体视为一个值
curl -s localhost:8000/merge -H 'content-type: application/json' -d '{
  "base":  {"a": 1, "nested": {"x": 1, "y": 2}, "tags": [1, 2]},
  "left":  {"a": 2, "nested": {"x": 1, "y": 2, "z": 3}, "tags": [1, 2]},
  "right": {"a": 1, "nested": {"x": 1, "y": 20}, "tags": [1, 3]}
}'
# 无冲突 => {"merged": {...}}；把两侧对同一路径改成不同值（或一侧删除一侧修改）
# 则返回 {"conflicts": [...]}，按 RFC 6901 路径排序，每条含 base/left/right 的 present+value
```

## 本地开发与测试（不使用 Docker）

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q          # 120+ 个测试
python3 -m uvicorn app.main:app --reload --port 8000
```

## 项目结构

```
app/
  main.py        # FastAPI 路由：/commit、/verify、/diff、/patch、/redact、/shape、/merge、错误处理
  diff.py        # 叶级路径对齐、规范值指纹与 added/removed/changed
  merge.py       # base/left/right 三路合并：对象成员递归、数组原子、规范相等、RFC 6901 排序冲突
  patch.py       # RFC 6902 add/remove/replace 引擎（有序执行、整体校验、changedPaths）
  redact.py      # 叶级脱敏引擎：RFC 6901 片段（字面量/*/**）匹配、mask/drop、规则优先级
  shape.py       # 结构清单与自底向上同形哈希（域标签 + 长度前缀，标量值不参与）
  tree.py        # 叶展开、叶哈希/节点哈希、排序建树、标准与紧凑证明
  pointers.py    # RFC 6901 指针解析与寻址
  canonical.py   # 规范 JSON 序列化
  jsonio.py      # 严格 JSON 解析（拒绝重复键、错误定位）
  validation.py  # 提交值校验（非有限数、非 JSON 类型等）
  openapi.py     # 两种证明格式与 /diff、/patch 的 OpenAPI 模型与文档合并
  errors.py      # 带 code/loc 的 API 错误类型
tests/           # 哈希/树单元测试 + API 端到端（含全部篡改、去重、结构错误与补丁场景）
Dockerfile
docker-entrypoint.sh   # 启动时安装依赖并启动 uvicorn
docker-compose.yml
requirements.txt
```
