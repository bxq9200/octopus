import { Node, Edge } from 'reactflow'
import { WorkflowNode } from '@/types'

/**
 * DAG 自动布局算法 — 从左到右水平布局
 *
 * 策略：
 * 1. 按依赖关系分层（拓扑排序）
 * 2. 同一层的节点垂直排列（前置任务在上）
 * 3. 层与层之间水平间距固定
 * 4. 边的交叉最小化
 * 5. 子节点（如子分镜）在父节点下方展开
 */

interface LayoutOptions {
  nodeWidth?: number
  nodeHeight?: number
  levelGap?: number      // 层间距（水平）
  siblingGap?: number    // 同层节点间距（垂直）
  startX?: number
  startY?: number
  excludeTypes?: string[]  // 0.7.x：网格模式下排除已合并进"分镜卡"的节点类型，不再 emit 成 custom 节点
}

const DEFAULT_OPTIONS: LayoutOptions = {
  nodeWidth: 260,
  nodeHeight: 240,
  levelGap: 260,        // 水平间距（层与层之间，给参数 / 结果连接点留空间）
  siblingGap: 64,       // 垂直间距（同层节点之间）— 给媒体节点留缓冲，避免 9:16 keyframe / video 行间挤压
  startX: 80,
  startY: 80,
}

// 节点宽度去掉左右 padding 后的图片/视频可用宽度
const MEDIA_WIDTH = 228
// 节点头部 + footer + 类型标签 + 已确认条 + result wrapper padding/margin 等固定开销
// 实测：py-3(24) + border(4) + header(~48) + type label(~22) + result p-2/mb-2(24) + 已确认 pill(~24) ≈ 146；
// keyframe 还会多渲染"融合 N 张参考图"文字 → 再 +20 ≈ 166。统一按 160 估，少量富余比频繁重叠更安全。
const CHROME = 160
// keyframe 节点：多一行"融合 N 张参考图"标签
const CHROME_KEYFRAME = CHROME + 20

// 从 "WxH" 字符串估算媒体在节点里的渲染高度
function mediaHeightFromResolution(resolution: string | undefined, fallbackRatio: number): number {
  if (typeof resolution === 'string') {
    const m = resolution.match(/^(\d+)\s*[x×]\s*(\d+)$/i)
    if (m) {
      const w = Number(m[1])
      const h = Number(m[2])
      if (w > 0 && h > 0) return Math.round(MEDIA_WIDTH * (h / w))
    }
  }
  return Math.round(MEDIA_WIDTH * fallbackRatio)
}

// 不同 node type 的实际渲染高度估算（基于节点宽 260、媒体可用宽 228）
function estimateNodeHeight(node: WorkflowNode): number {
  const t = node.type
  const r = (node.result || {}) as any

  if (t === 'result') {
    // 子节点：角色（正面 1024² + 三视图 1344x768）或场景（单图 1024²）
    if (r.image_url && r.three_view_image_url) {
      return CHROME + MEDIA_WIDTH + Math.round(MEDIA_WIDTH * (768 / 1344)) + 24
    }
    if (r.image_url) return CHROME + MEDIA_WIDTH + 16
    return 130
  }

  if (t === 'script_generation') return 230
  if (t === 'art_design') return 240
  if (t === 'storyboard_design') return 180

  if (t === 'keyframe_generation') {
    // 关键帧节点：image_to_image 融合图，按 params.width/height 估算
    const w = (node.params as any)?.width || 720
    const h = (node.params as any)?.height || 1280
    const ratio = h / w
    return CHROME_KEYFRAME + Math.round(MEDIA_WIDTH * ratio)
  }

  if (t === 'video_generation') {
    // 视频按节点的 resolution 参数估算；默认按竖屏 9:16 留高
    const reso = (node.params as any)?.resolution || (r.resolution as string | undefined)
    return CHROME + mediaHeightFromResolution(reso, 16 / 9)
  }

  if (t === 'video_merge') {
    const reso = (node.params as any)?.resolution || (r.resolution as string | undefined)
    return CHROME + mediaHeightFromResolution(reso, 16 / 9)
  }

  if (t === 'publish') return 200

  // 0.6.1: 音频规划节点（无媒体，主要展示 mood + 段数）
  if (t === 'audio_plan') return 180

  // 0.6.1: BGM 节点（音频播放器约 50px）
  if (t === 'bgm_fetch') {
    return r.public_url ? 220 : 160
  }

  return 200
}

/**
 * 拓扑排序 + 分层布局（水平方向）
 */
export function layoutDAG(nodes: WorkflowNode[], options: LayoutOptions = {}): Node[] {
  const opts = { ...DEFAULT_OPTIONS, ...options }

  // 0.7.x 网格模式：排除已被合并进"分镜卡"的节点（keyframe/video）→ 这两类不再单独 emit。
  // 过滤在最顶部，自然让下游的 sbLevel 覆盖、shot_index 行对齐都 no-op（找不到这些节点）。
  if (options.excludeTypes && options.excludeTypes.length) {
    const ex = new Set(options.excludeTypes)
    nodes = nodes.filter((n) => !ex.has(n.type))
  }

  // 构建依赖图
  const nodeMap = new Map<string, WorkflowNode>()
  nodes.forEach(n => nodeMap.set(n.id, n))

  // 计算每个节点的层级（最长路径）
  const levels = new Map<string, number>()

  function getLevel(nodeId: string): number {
    if (levels.has(nodeId)) return levels.get(nodeId)!

    const node = nodeMap.get(nodeId)
    if (!node) {
      // 悬空依赖：引用了不存在的节点（如链首 video 尚未创建却被 link 的 depends_on 引用）
      // → 当 0 处理但**不写入 levels**，避免幽灵 id 进 levelGroups 导致渲染崩溃
      return 0
    }
    if (!node.depends_on || node.depends_on.length === 0) {
      levels.set(nodeId, 0)
      return 0
    }

    const maxDepLevel = Math.max(...node.depends_on.map(depId => getLevel(depId)))
    const level = maxDepLevel + 1
    levels.set(nodeId, level)
    return level
  }

  // 计算所有节点的层级
  nodes.forEach(n => getLevel(n.id))

  // 0.7.1：keyframe / video **强制各占独立一列**（在 storyboard 之后）。否则链式 eager 视频
  // 依赖 storyboard(非 keyframe) → 落到关键帧同列甚至更左，导致"关键帧/视频"分区带 X 重叠、排序乱。
  const sbLevels = nodes.filter(n => n.type === 'storyboard_design').map(n => levels.get(n.id) ?? 0)
  const sbLevel = sbLevels.length ? Math.max(...sbLevels) : -1
  if (sbLevel >= 0) {
    nodes.forEach(n => {
      if (n.type === 'keyframe_generation') levels.set(n.id, sbLevel + 1)
      else if (n.type === 'video_generation' || n.type === 'video_merge') levels.set(n.id, sbLevel + 2)
    })
  }

  // 按层级分组
  const levelGroups = new Map<number, string[]>()
  levels.forEach((level, nodeId) => {
    if (!levelGroups.has(level)) levelGroups.set(level, [])
    levelGroups.get(level)!.push(nodeId)
  })

  // 计算位置 — 每层独立累加各节点的真实高度，避免重叠
  const maxLevel = Math.max(...Array.from(levels.values()))
  const levelHeights: number[] = []
  const nodeHeights = new Map<string, number>()
  nodes.forEach(n => nodeHeights.set(n.id, estimateNodeHeight(n)))

  // 计算每层的总高度（按真实节点高度累加）
  for (let i = 0; i <= maxLevel; i++) {
    const group = levelGroups.get(i) || []
    const sum = group.reduce((acc, id) => acc + (nodeHeights.get(id) ?? opts.nodeHeight!), 0)
    const height = sum + Math.max(0, group.length - 1) * opts.siblingGap!
    levelHeights.push(height)
  }

  const maxHeight = Math.max(...levelHeights, 0)

  const rfNodes: Node[] = []

  for (let level = 0; level <= maxLevel; level++) {
    const group = levelGroups.get(level) || []
    const levelHeight = levelHeights[level]
    let cursorY = opts.startY! + (maxHeight - levelHeight) / 2  // 垂直居中

    group.forEach((nodeId) => {
      const node = nodeMap.get(nodeId)
      if (!node) return  // 跳过悬空依赖引用的幽灵 id（如链首 video 尚未创建却被 link 的 depends_on 引用）
      const h = nodeHeights.get(nodeId) ?? opts.nodeHeight!
      const x = opts.startX! + level * (opts.nodeWidth! + opts.levelGap!)
      const y = cursorY
      cursorY += h + opts.siblingGap!

      rfNodes.push({
        id: node.id,
        type: 'custom',
        position: { x, y },
        data: {
          type: node.type,
          label: node.label,
          status: node.status,
          params: node.params,
          inputs: node.inputs,
          outputs: node.outputs,
          result: node.result,
          error: node.error,
          agent: node.agent,
          module: node.module,
          needUserReview: node.need_user_review,
          need_confirm: node.need_confirm,
          confirmed: node.confirmed,
          children: node.children,
          depends_on: node.depends_on,
          progress: (node as any).progress,
          progress_message: (node as any).progress_message,
          estHeight: h,   // 0.7.1：给分区背景带算包围盒高度用
        },
      })
    })
  }

  // 0.7.1：关键帧 + 视频按 shot_index 排成"每镜一行" —— keyframe[N] 与 video[N] 同 Y（链式 eager 视频也各归其行），
  // keyframe→video 连线成水平短线、互不重叠。其余列(美术/storyboard)保持原布局。
  const shotNodes = rfNodes.filter((n) =>
    (n.data as any)?.type === 'keyframe_generation' || (n.data as any)?.type === 'video_generation',
  )
  const shotIdxs = Array.from(new Set(
    shotNodes.map((n) => (n.data as any)?.params?.shot_index).filter((v): v is number => typeof v === 'number'),
  )).sort((a, b) => a - b)
  if (shotIdxs.length) {
    let rowY = opts.startY!
    const rowYOf = new Map<number, number>()
    shotIdxs.forEach((si) => {
      rowYOf.set(si, rowY)
      const rowH = Math.max(
        ...shotNodes
          .filter((n) => (n.data as any)?.params?.shot_index === si)
          .map((n) => ((n.data as any)?.estHeight as number) || 220),
      )
      rowY += rowH + opts.siblingGap!
    })
    shotNodes.forEach((n) => {
      const si = (n.data as any)?.params?.shot_index
      if (typeof si === 'number' && rowYOf.has(si)) n.position = { ...n.position, y: rowYOf.get(si)! }
    })
  }

  return rfNodes
}

// ═══════════════════════════════════════════════════════
// 0.7.1：阶段分区背景带（轻量"组"——美术 / 关键帧 / 视频 各一条带标题的半透明底）
// ═══════════════════════════════════════════════════════
const PHASE_BAND_META: Record<string, { label: string; bg: string; border: string; text: string }> = {
  art:      { label: '🎨 美术素材', bg: 'rgba(168,85,247,0.05)', border: 'rgba(168,85,247,0.22)', text: '#c084fc' },
  keyframe: { label: '🖼 关键帧',   bg: 'rgba(234,179,8,0.05)',  border: 'rgba(234,179,8,0.22)',  text: '#eab308' },
  video:    { label: '🎬 视频',     bg: 'rgba(245,158,11,0.05)', border: 'rgba(245,158,11,0.22)', text: '#f59e0b' },
}
function phaseOf(type?: string): string | null {
  if (type === 'keyframe_generation') return 'keyframe'
  if (type === 'video_generation' || type === 'video_merge') return 'video'
  if (type === 'result' || type === 'art_design' || type === 'episode_art_design') return 'art'
  return null
}
/** 从已布局节点算出每个阶段的包围盒，生成背景带节点（zIndex 压在普通节点后面、不拦点击）。 */
export function generatePhaseBands(rfNodes: Node[]): Node[] {
  const PAD = 40
  const LABEL_H = 26
  const box: Record<string, { minX: number; maxX: number; minY: number; maxY: number; n: number }> = {}
  rfNodes.forEach((n) => {
    const ph = phaseOf((n.data as any)?.type)
    if (!ph) return
    const h = ((n.data as any)?.estHeight as number) || 220
    const b = box[ph] || { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity, n: 0 }
    b.minX = Math.min(b.minX, n.position.x)
    b.maxX = Math.max(b.maxX, n.position.x + 260)
    b.minY = Math.min(b.minY, n.position.y)
    b.maxY = Math.max(b.maxY, n.position.y + h)
    b.n += 1
    box[ph] = b
  })
  const bands: Node[] = []
  for (const ph of Object.keys(box)) {
    const b = box[ph]
    if (b.n < 2) continue // 单节点不画带
    const meta = PHASE_BAND_META[ph]
    bands.push({
      id: `band-${ph}`,
      type: 'phaseBand',
      position: { x: b.minX - PAD, y: b.minY - PAD - LABEL_H },
      data: {
        label: meta.label, count: b.n, bg: meta.bg, border: meta.border, text: meta.text,
        w: b.maxX - b.minX + PAD * 2,
        h: b.maxY - b.minY + PAD * 2 + LABEL_H,
      },
      draggable: false,
      selectable: false,
      zIndex: -1,
    } as Node)
  }
  return bands
}

// ═══════════════════════════════════════════════════════
// 0.7.x：分镜卡网格 —— 每镜的 keyframe + video 合并成一张合成卡（纯前端 view，后端零改），
// 横排折行成近方形网格。卡片固定尺寸 + 缩略图 object-cover 裁切 → 布局高度与 9:16 媒体比例解耦，
// 干掉"69 镜单列 2 万 px 长柱"。所有动作仍走真实节点 id。
// ═══════════════════════════════════════════════════════
const SHOT_CARD_W = 240
const SHOT_CARD_H = 200
const SHOT_GAP_X = 28
const SHOT_GAP_Y = 36
const SHOT_MAX_COLS = 8

export interface ShotCardsResult {
  cards: Node[]
  gridBox: { x: number; y: number; w: number; h: number } | null
  cardOfNode: Map<string, string> // 真实 keyframe/video 节点 id → 所属 shot-<idx> 卡 id
}

/** 按 params.shot_index 把 keyframe / video 节点配对成镜头组（ShotNavigator 与网格共用）。 */
export function groupByShot(
  nodes: WorkflowNode[],
): Map<number, { kf?: WorkflowNode; video?: WorkflowNode }> {
  const byShot = new Map<number, { kf?: WorkflowNode; video?: WorkflowNode }>()
  for (const n of nodes) {
    if (n.type !== 'keyframe_generation' && n.type !== 'video_generation') continue
    const si = (n.params as any)?.shot_index
    if (typeof si !== 'number') continue
    const e = byShot.get(si) || {}
    if (n.type === 'keyframe_generation') e.kf = n
    else e.video = n
    byShot.set(si, e)
  }
  return byShot
}

function pickShotLabel(kf?: WorkflowNode, video?: WorkflowNode, si?: number): string {
  const raw = kf?.label || video?.label || ''
  const m = raw.match(/SHOT[_ ]?(\d+)/i)
  if (m) return `SHOT_${m[1].padStart(2, '0')}`
  return `SHOT_${String((si ?? 0) + 1).padStart(2, '0')}`
}

/** 生成分镜卡合成节点 + 网格包围盒 + (真实 id→卡 id) 映射。origin = 网格左上角。 */
export function buildShotCards(
  nodes: WorkflowNode[],
  origin: { x: number; y: number } = { x: 80, y: 80 },
  parentId?: string,   // 传了 → 子卡作为该容器(grid-group)的子节点：相对坐标 + extent:'parent'
): ShotCardsResult {
  const byShot = groupByShot(nodes)
  const idxs = Array.from(byShot.keys()).sort((a, b) => a - b)
  const cardOfNode = new Map<string, string>()
  if (!idxs.length) return { cards: [], gridBox: null, cardOfNode }
  const LABEL_PAD = parentId ? 48 : 0   // 容器内顶部给「镜头网格」标题 + 留白
  const PAD = parentId ? 24 : 0          // 容器左右下四周留白，卡片不贴边框

  const N = Math.min(SHOT_MAX_COLS, Math.max(1, Math.ceil(Math.sqrt(idxs.length))))
  const cards: Node[] = []
  idxs.forEach((si, i) => {
    const { kf, video } = byShot.get(si)!
    const col = i % N
    const row = Math.floor(i / N)
    const cardId = `shot-${si}`
    if (kf) cardOfNode.set(kf.id, cardId)
    if (video) cardOfNode.set(video.id, cardId)
    const v = (video?.params as any) || {}
    const snap = (n?: WorkflowNode) =>
      n
        ? {
            id: n.id,
            status: n.status,
            confirmed: n.confirmed,
            result: n.result,
            progress: (n as any).progress,
            progress_message: (n as any).progress_message,
            // 视频首帧/链式尾帧：优先输出帧，回退到输入首帧(=关键帧图 或 上一镜尾帧)
            firstFrame:
              (n.result as any)?.first_frame_url ||
              (n.result as any)?.cover_url ||
              (n.params as any)?.first_frame_url ||
              null,
            // 链镜首帧是否已是"真尾帧"(生成时由 _resolve_chain_first_frame 注入)。false=还是 fan-out 兜底场景图
            firstFrameFromChain: !!(n.params as any)?.first_frame_from_chain,
          }
        : null
    const card: any = {
      id: cardId,
      type: 'shotCard',
      position: { x: origin.x + PAD + col * (SHOT_CARD_W + SHOT_GAP_X), y: origin.y + LABEL_PAD + row * (SHOT_CARD_H + SHOT_GAP_Y) },
      width: SHOT_CARD_W,
      height: SHOT_CARD_H,
      data: {
        shotIndex: si,
        shotLabel: pickShotLabel(kf, video, si),
        chainRole: v.chain_role ?? null,
        chainHeadIndex: v.chain_head_index ?? null,
        // 0.7.2：项目画面比例（16:9 时卡片内部改上下两半，竖图左右半区会被裁成细条）
        aspect: v.aspect_ratio || ((kf?.params as any) || {}).aspect_ratio || '',
        kf: snap(kf),
        video: snap(video),
      },
      draggable: true,
      // selectable:true 必须留。draggable 改 true 后卡片可拖动；内部交互元素（半区/按钮/▶）须加 `nodrag`
      // className，否则点按钮会误触发拖动。内部 onClick 仍 stopPropagation 阻止 RF 实选合成节点。
      selectable: true,
    }
    // subflow：作为 grid-group 的子节点，拖容器由 RF 原生带动；extent 限制卡在容器内
    if (parentId) { card.parentNode = parentId; card.extent = 'parent' }
    cards.push(card as Node)
  })

  const cols = Math.min(N, idxs.length)
  const rows = Math.ceil(idxs.length / N)
  const gridBox = {
    x: origin.x,
    y: origin.y,
    w: PAD * 2 + cols * SHOT_CARD_W + (cols - 1) * SHOT_GAP_X,
    h: LABEL_PAD + rows * SHOT_CARD_H + (rows - 1) * SHOT_GAP_Y + PAD,
  }
  return { cards, gridBox, cardOfNode }
}

/** 镜头网格组容器（subflow parent）：可拖、可连线。position=容器在画布的绝对位置，宽高=gridBox。
 *  分镜卡用 parentNode='grid-group' 挂在它下面，拖容器由 RF 原生带动所有子卡。 */
export function buildShotGroupNode(
  id: string,
  gridBox: { x: number; y: number; w: number; h: number } | null,
  position: { x: number; y: number },
  count: number,
  label = '🎬 镜头网格',
): Node[] {
  if (!gridBox) return []
  return [
    {
      id,
      type: 'shotGroup',
      position,
      style: { width: gridBox.w, height: gridBox.h },
      width: gridBox.w,
      height: gridBox.h,
      data: { count, label },
      draggable: true,
      selectable: false,
    } as Node,
  ]
}

/** 美术素材组容器（对称镜头网格）：把美术子图(result)网格排进 art-group 容器(parentNode + 相对坐标)。
 *  美术设计主节点(art_design)留在组外、连到容器。返回 {group:[容器], children:[子图 rfNode]}。 */
export function buildArtGroup(
  nodes: WorkflowNode[],
  groupId: string,
  position: { x: number; y: number },
  label: string,
): { group: Node[]; children: Node[] } {
  const arts = nodes.filter((n) => n.type === 'result')
  if (!arts.length) return { group: [], children: [] }
  const N = Math.min(3, Math.max(1, Math.ceil(Math.sqrt(arts.length))))
  // 组内美术卡用 data.fixedWidth 锁定 260px 宽（CustomNode 默认 min-w 220~max-w 300 随内容浮动，
  // 浮动宽度会让列对不齐、容器左右留白不对称）。COL_W = 卡宽，留白由构造对称。
  const COL_W = 260, GAP_X = 40, GAP_Y = 40, PAD = 28, LABEL_PAD = 52
  // 美术卡专用估高：头部/标签/确认条 ~180 + 主图(媒体宽228按1:1) + 三视图条(~130，通用 estimateNodeHeight 不算这张 → 之前行重叠/框不住的根因)
  const estArt = (n: WorkflowNode) => {
    const r: any = n.result || {}
    const hasThree = !!(r.three_view_image_url || r.composite_reference_url)
    return 180 + 228 + (hasThree ? 130 : 0)
  }
  // 高卡(带三视图的角色)排前面 → 同行高度接近、行高利用率高
  arts.sort((a, b) => estArt(b) - estArt(a))
  // 行高对齐：每行取该行最高节点
  const rowH: number[] = []
  arts.forEach((n, i) => {
    const r = Math.floor(i / N)
    rowH[r] = Math.max(rowH[r] || 0, estArt(n))
  })
  const rowY: number[] = []
  let y = LABEL_PAD
  rowH.forEach((h, r) => { rowY[r] = y; y += h + GAP_Y })
  const children: Node[] = arts.map((n, i) => {
    const col = i % N, row = Math.floor(i / N)
    return {
      id: n.id,
      type: 'custom',
      position: { x: PAD + col * (COL_W + GAP_X), y: rowY[row] },
      parentNode: groupId,
      extent: 'parent',
      draggable: true,
      selectable: true,
      data: {
        type: n.type, label: n.label, status: n.status, params: n.params,
        inputs: n.inputs, outputs: n.outputs, result: n.result, error: n.error,
        agent: n.agent, module: n.module, needUserReview: n.need_user_review,
        need_confirm: n.need_confirm, confirmed: n.confirmed,
        children: n.children, depends_on: n.depends_on,
        progress: (n as any).progress, progress_message: (n as any).progress_message,
        estHeight: estArt(n),
        fixedWidth: true,   // 组内锁定 260px 宽，列对齐 + 容器留白对称
      },
    } as Node
  })
  const cols = Math.min(N, arts.length)
  const w = PAD * 2 + cols * COL_W + (cols - 1) * GAP_X
  const h = y - GAP_Y + PAD
  const group: Node[] = [{
    id: groupId, type: 'shotGroup', position,
    style: { width: w, height: h }, width: w, height: h,
    data: { count: arts.length, label },
    draggable: true, selectable: false,
  } as Node]
  return { group, children }
}

/** 分镜卡之间的连线：只画连贯链（亮琥珀，尾帧续接的相邻镜）。
 *  顺序流不画 —— 网格的位置+编号已表达阅读顺序，68 条顺序线在折行处交叉成网反而乱。 */
export function buildShotCardEdges(nodes: WorkflowNode[]): Edge[] {
  const byShot = groupByShot(nodes)
  const idxs = Array.from(byShot.keys()).sort((a, b) => a - b)
  if (idxs.length < 2) return []
  const edges: Edge[] = []
  // 连贯链（亮）：尾帧续接的相邻镜
  const sb = nodes.find((n) => n.type === 'storyboard_design')
  const chains: number[][] = ((sb?.params as any)?.continuity_chains as number[][]) || []
  const have = new Set(idxs)
  chains.forEach((chain, ci) => {
    for (let k = 0; k + 1 < (chain?.length || 0); k++) {
      const a = chain[k]
      const b = chain[k + 1]
      if (!have.has(a) || !have.has(b)) continue
      edges.push({
        id: `chain-${ci}-${a}-${b}`,
        source: `shot-${a}`,
        target: `shot-${b}`,
        type: 'smoothstep',
        animated: false,
        className: 'chain-edge',
        style: { stroke: '#f59e0b', strokeWidth: 2, filter: 'drop-shadow(0 0 3px rgba(245,158,11,0.7))' },
        markerEnd: { type: 'arrowclosed', color: '#f59e0b' } as any,
        zIndex: 5,
      } as Edge)
    }
  })
  return edges
}

/** 网格区域背景带（像 phaseBand 一样压在后面、不拦点击）。 */
export function buildGridRegionBand(
  gridBox: { x: number; y: number; w: number; h: number } | null,
  count: number,
): Node[] {
  if (!gridBox) return []
  const PAD = 36
  const LABEL_H = 26
  return [
    {
      id: 'band-shots',
      type: 'phaseBand',
      position: { x: gridBox.x - PAD, y: gridBox.y - PAD - LABEL_H },
      data: {
        label: '🎬 镜头网格',
        count,
        bg: 'rgba(245,158,11,0.04)',
        border: 'rgba(245,158,11,0.18)',
        text: '#f59e0b',
        w: gridBox.w + PAD * 2,
        h: gridBox.h + PAD * 2 + LABEL_H,
      },
      draggable: false,
      selectable: false,
      zIndex: -1,
    } as Node,
  ]
}

/**
 * 从后端 workflow_nodes 转换为前端 WorkflowNode
 */
export function convertWorkflowNodes(backendNodes: any[]): WorkflowNode[] {
  return backendNodes.map((n, index) => ({
    id: n.node_id || n.id,
    type: n.node_type || n.type,
    label: n.node_name || n.label,
    position: n.position || { x: 0, y: 0 },
    params: n.params || n.input || {},
    inputs: n.inputs || {},
    outputs: n.outputs || {},
    depends_on: n.depends_on || [],
    children: n.children || [],
    status: n.status || 'idle',
    result: n.result,
    error: n.error,
    agent: n.agent,
    module: n.module,
    need_user_review: n.need_user_review,
    need_confirm: n.need_confirm !== false,
    confirmed: n.confirmed || false,
    output: n.output || {},
    next_nodes: n.next_nodes || [],
    parent_node: n.parent_node,
    has_children: n.has_children,
  }))
}

/**
 * 生成边
 *
 * 规则：
 * - 节点左侧 Handle = 参数输入（每个 depends_on 一个，id="param_${index}"）
 * - 节点右侧 Handle = 结果输出
 *   * 没有 children 的节点：单一无 id Handle（默认输出）
 *   * 有 children 的节点：每个 child 一个 Handle，id="result_${childIdx}"
 *
 * 因此 source handle 必须按以下规则匹配：
 * - 如果 source 节点有 children 且 target 在其 children 中 → sourceHandle="result_${idx}"
 * - 否则 → sourceHandle=undefined（匹配默认无 id 的 Handle）
 *
 * 颜色：
 * - 蓝色实线：常规依赖（script→art、art→storyboard 等）
 * - 绿色虚线：父→子结果（art→character_image、storyboard→shot 等）
 */
// 边视觉分类：running=强光电 / 排队待跑(父就绪 + 本节点 idle)=淡青呼吸 / 其它=冷白静线
const _DONE = new Set(['confirmed', 'completed'])
function edgeVisual(sourceStatus?: string, targetStatus?: string) {
  const active = targetStatus === 'running'
  const queued = !active && (!targetStatus || targetStatus === 'idle') && _DONE.has(sourceStatus || '')
  if (active) {
    return {
      animated: true,
      className: undefined as string | undefined,
      style: { stroke: '#22d3ee', strokeWidth: 2, filter: 'drop-shadow(0 0 4px rgba(34,211,238,0.9))' },
      markerColor: '#22d3ee',
    }
  }
  if (queued) {
    return {
      animated: false,
      className: 'edge-queued' as string | undefined,
      // 0.7.1：排队连线压淡（70+ 镜时大量 idle 连线成团），保留淡青呼吸但不抢戏
      style: { stroke: 'rgba(34, 211, 238, 0.22)', strokeWidth: 1 },
      markerColor: 'rgba(34,211,238,0.28)',
    }
  }
  return {
    animated: false,
    className: undefined as string | undefined,
    // 0.7.1：静态连线压到极淡 → 成背景纹理，只让 running 高亮，一眼看清当前流向
    style: { stroke: 'rgba(214, 222, 244, 0.10)', strokeWidth: 1 },
    markerColor: 'rgba(214,222,244,0.14)',
  }
}

export function generateEdges(nodes: WorkflowNode[]): Edge[] {
  const edges: Edge[] = []
  const nodeById = new Map<string, WorkflowNode>()
  nodes.forEach(n => nodeById.set(n.id, n))

  nodes.forEach((node) => {
    node.depends_on?.forEach((depId: string, index: number) => {
      const v = edgeVisual(nodeById.get(depId)?.status, node.status)
      edges.push({
        id: `edge-${depId}-${node.id}-${index}`,
        source: depId,
        target: node.id,
        animated: v.animated,
        className: v.className,
        type: 'smoothstep',
        style: v.style,
        markerEnd: { type: 'arrowclosed', color: v.markerColor },
      } as Edge)
    })

    // 兜底：parent.children 中存在但 child 节点不在 nodes 数组里的情况（基本不会发生）
    node.children?.forEach((childId: string, index: number) => {
      if (!nodeById.has(childId)) return
      const child = nodeById.get(childId)!
      // 已经从 child.depends_on 那边生成过边了，跳过
      if (child.depends_on?.includes(node.id)) return
      const v = edgeVisual(node.status, child.status)
      edges.push({
        id: `edge-child-${node.id}-${childId}`,
        source: node.id,
        target: childId,
        animated: v.animated,
        className: v.className,
        type: 'smoothstep',
        style: v.style,
        markerEnd: { type: 'arrowclosed', color: v.markerColor },
      } as Edge)
    })
  })

  return edges
}
