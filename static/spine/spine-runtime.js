// Spine 多版本运行时加载器 + API 适配层。
//
// 为什么需要:Spine 的骨架数据是**版本锁定**的——3.8 导出的 .skel 只能用 3.8 运行时读,
// 4.1 运行时会直接抛 "Skeleton version ... not supported"。所以想同时播放不同版本的
// 资源,就必须同时具备多份运行时。
//
// 做法:把各版本的预编译 bundle 放在 public/spine-runtime/<id>/spine-webgl.js,
// 按需 fetch 后用 `new Function(src + ';return spine;')()` 求值。各版本 bundle 顶层都是
// `var spine = ...`(3.8 是 TS 命名空间输出,4.x 是 esbuild 的 iife 输出),`var` 属于函数作用域,
// 因此在 Function 体内求值天然互相隔离,不会污染 window.spine、也不会互相覆盖。
//
// 版本间的 API 差异由 adapt() 抹平:
//   3.8    WebGL 类在 spine.webgl.* 下;AssetManager 只能轮询 isLoadingComplete();取资源用 get()
//   4.0/4.1  全部平铺在 spine.* 下;loadAll() 返回 Promise;取资源用 require()
//   4.2/4.3  同 4.x,但 skeleton.updateWorldTransform() 需要传 spine.Physics.update

// RUNTIMES 版本清单(id 为 "主.次",与骨架文件里记录的导出版本对齐)。
export const RUNTIMES = [
  { id: '3.8', label: '3.8.99', file: 'runtime/3.8/spine-webgl.js', ns: 'webgl', physics: false },
  { id: '4.0', label: '4.0.31', file: 'runtime/4.0/spine-webgl.js', ns: '', physics: false },
  { id: '4.1', label: '4.1.55', file: 'runtime/4.1/spine-webgl.js', ns: '', physics: false },
  { id: '4.2', label: '4.2.120', file: 'runtime/4.2/spine-webgl.js', ns: '', physics: true },
  { id: '4.3', label: '4.3.13', file: 'runtime/4.3/spine-webgl.js', ns: '', physics: true },
]

const cache = new Map() // id -> Promise<adapter>

// pickRuntime 把骨架文件里的导出版本(如 "3.8.99" / "4.1.24" / "4.4.x")映射到可用运行时 id。
// 只匹配主次版本。未知版本返回 null，由使用者手选；已知不支持版本明确拒绝。
export function pickRuntime(version) {
  const m = /^(\d+)\.(\d+)(?:\.|$)/.exec(String(version || '').trim())
  if (!m) return null
  const id = `${Number(m[1])}.${Number(m[2])}`
  if (RUNTIMES.some((r) => r.id === id)) return id
  throw new Error(`Spine ${version} 暂不支持。请使用 3.8、4.0、4.1、4.2 或 4.3 导出的资源。`)
}

// loadRuntime 加载(并缓存)指定版本的运行时适配器。
export function loadRuntime(id) {
  const spec = RUNTIMES.find((r) => r.id === id)
  if (!spec) return Promise.reject(new Error('未知的 Spine 运行时版本: ' + id))
  if (!cache.has(id)) {
    cache.set(
      id,
      fetchBundle(spec).catch((e) => {
        cache.delete(id) // 失败不缓存,允许重试
        throw e
      })
    )
  }
  return cache.get(id)
}

async function fetchBundle(spec) {
  const url = new URL(spec.file, import.meta.url).href
  const res = await fetch(url, { credentials: 'same-origin' })
  if (!res.ok) throw new Error(`运行时 ${spec.label} 加载失败: HTTP ${res.status} ${url}`)
  const src = await res.text()
  // bundle 顶层是 `var spine = ...`,在 Function 体内即为局部变量 —— 天然隔离多版本。
  let raw
  try {
    raw = new Function(`${src}\n;return typeof spine !== 'undefined' ? spine : null;`)()
  } catch (e) {
    throw new Error(`运行时 ${spec.label} 求值失败: ${e && e.message ? e.message : e}`)
  }
  if (!raw) throw new Error(`运行时 ${spec.label} 未导出 spine 命名空间`)
  return adapt(raw, spec)
}

// adapt 把某个版本的 spine 命名空间包成统一接口。
function adapt(raw, spec) {
  const gl = spec.ns === 'webgl' ? raw.webgl : raw
  if (!gl || !gl.ManagedWebGLRenderingContext) {
    throw new Error(`运行时 ${spec.label} 缺少 WebGL 模块(spine${spec.ns ? '.' + spec.ns : ''}.ManagedWebGLRenderingContext)`)
  }
  // 4.2+ 的 updateWorldTransform 需要 Physics 枚举
  const physicsMode = spec.physics && raw.Physics ? raw.Physics.update : null

  return {
    id: spec.id,
    label: spec.label,
    core: raw,
    webgl: gl,

    newContext: (canvas, config) => new gl.ManagedWebGLRenderingContext(canvas, config),
    newAssetManager: (ctx, pathPrefix) => new gl.AssetManager(ctx, pathPrefix),
    newTexturedShader: (ctx) => gl.Shader.newTwoColoredTextured(ctx),
    newColoredShader: (ctx) => gl.Shader.newColored(ctx),
    newBatcher: (ctx) => new gl.PolygonBatcher(ctx),
    newShapeRenderer: (ctx) => new gl.ShapeRenderer(ctx),
    newMatrix4: () => new gl.Matrix4(),
    newSkeletonRenderer: (ctx) => new gl.SkeletonRenderer(ctx),
    newDebugRenderer: (ctx) => new gl.SkeletonDebugRenderer(ctx),
    newTexture: (ctx, image) => new gl.GLTexture(ctx, image),
    newAtlas: (text, textureLoader) => spec.ns === 'webgl'
      ? new raw.TextureAtlas(text, textureLoader)
      : new raw.TextureAtlas(text),
    newFakeTexture: (image) => new raw.FakeTexture(image),
    samplerUniform: gl.Shader.SAMPLER,
    mvpUniform: gl.Shader.MVP_MATRIX,

    // loadAll 等待 AssetManager 把队列里的资源全部下载完。
    //
    // 这里刻意不用 4.x 自带的 loadAll():它内部是 requestAnimationFrame 轮询,
    // 浏览器把后台/不可见标签页的 rAF 冻结后,即使资源早就下载完了 Promise 也永远不 resolve。
    // 换成 setTimeout 轮询,切走标签页再切回来也不会卡在"加载中"。
    // 3.8 本来就没有 loadAll(),只能轮询,这样两边正好统一。
    async loadAll(am, timeoutMs = 30000) {
      await new Promise((resolve, reject) => {
        const t0 = Date.now()
        const tick = () => {
          if (am.isLoadingComplete()) {
            if (am.hasErrors && am.hasErrors()) reject(new Error(formatAssetErrors(am.getErrors())))
            else resolve()
            return
          }
          if (Date.now() - t0 > timeoutMs) {
            reject(new Error(`资源加载超时(还剩 ${am.getToLoad()} 项)`))
            return
          }
          setTimeout(tick, 30)
        }
        tick()
      })
    },

    // disposeAssets 释放图集纹理。
    //
    // 官方 dispose() 在 4.2/4.3 上会抛 TypeError:AssetManagerBase.remove() 又给
    // "已带前缀"的路径拼了一次 pathPrefix,取到 undefined 再读 .dispose 就炸了;
    // removeAll() 因此在第一个图集页上中断,后面的纹理全漏掉(spine-ts 上游问题)。
    // 所以先按官方路径试,抛了就自己遍历资源表直接删底层 WebGLTexture 兜底。
    disposeAssets(am, ctx) {
      if (!am) return
      try {
        am.dispose()
        return
      } catch {}
      const assets = (am.cache && am.cache.assets) || am.assets || {}
      const gl = ctx && ctx.gl
      for (const key of Object.keys(assets)) {
        const asset = assets[key]
        const tex = asset && asset.texture
        if (gl && tex && typeof WebGLTexture !== 'undefined' && tex instanceof WebGLTexture) {
          try {
            gl.deleteTexture(tex)
          } catch {}
        }
      }
    },

    // getAsset 4.x 用 require()(缺失会抛错),3.8 只有 get()。
    getAsset(am, path) {
      const v = typeof am.require === 'function' ? am.require(path) : am.get(path)
      if (!v) throw new Error('资源未加载: ' + path)
      return v
    },

    // readSkeletonData 用图集构造 Attachment 加载器,再按二进制/JSON 解析骨架数据。
    readSkeletonData(atlas, data, binary, scale) {
      const atlasLoader = new raw.AtlasAttachmentLoader(atlas)
      const loader = binary ? new raw.SkeletonBinary(atlasLoader) : new raw.SkeletonJson(atlasLoader)
      loader.scale = scale || 1
      return loader.readSkeletonData(data)
    },
    newSkeleton: (skeletonData) => new raw.Skeleton(skeletonData),
    newAnimationState(skeletonData) {
      const state = new raw.AnimationState(new raw.AnimationStateData(skeletonData))
      // Spine 4.3 renamed getCurrent to getTrack. Keep one interface for native/UI controls.
      if (typeof state.getCurrent !== 'function' && typeof state.getTrack === 'function') {
        state.getCurrent = (track) => state.getTrack(track)
      }
      return state
    },

    // updateWorldTransform 4.2+ 必须传 Physics 模式,3.8/4.0/4.1 不接受参数。
    updateWorldTransform(skeleton) {
      if (physicsMode !== null) skeleton.updateWorldTransform(physicsMode)
      else skeleton.updateWorldTransform()
    },

    // setupPose / setupPoseSlots
    // 4.3 把这组方法改名了:setToSetupPose -> setupPose,setSlotsToSetupPose -> setupPoseSlots。
    setupPose(skeleton) {
      if (typeof skeleton.setToSetupPose === 'function') skeleton.setToSetupPose()
      else skeleton.setupPose()
    },
    setupPoseSlots(skeleton) {
      if (typeof skeleton.setSlotsToSetupPose === 'function') skeleton.setSlotsToSetupPose()
      else skeleton.setupPoseSlots()
    },

    // getBounds 返回 setup pose 的包围盒,用于取景居中。第三个参数在 3.8 是必填的。
    getBounds(skeleton) {
      const offset = new raw.Vector2()
      const size = new raw.Vector2()
      skeleton.getBounds(offset, size, [])
      return { offset, size }
    },
  }
}

// formatAssetErrors 4.x 的 loadAll() reject 的是 {path: message} 字典而不是 Error。
function formatAssetErrors(errs) {
  if (!errs) return '资源加载失败'
  if (errs instanceof Error) return errs.message
  if (typeof errs === 'string') return errs
  const parts = Object.keys(errs).map((k) => `${k}: ${errs[k]}`)
  return parts.length ? parts.join('; ') : '资源加载失败'
}
