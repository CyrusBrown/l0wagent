import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import { ThreeMFLoader } from 'three/examples/jsm/loaders/3MFLoader.js'

/** STL, 3MF and STEP in an orbitable viewer. STEP is converted in the browser with
 * occt-import-js (a WebAssembly build of OpenCascade), loaded only when needed. */
export default function Model3D({ url, name }: { url: string; name: string }) {
  const box = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<'loading' | 'ok' | string>('loading')

  useEffect(() => {
    const el = box.current
    if (!el) return
    let disposed = false
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setSize(el.clientWidth, el.clientHeight)
    el.appendChild(renderer.domElement)
    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(40, el.clientWidth / el.clientHeight, 0.1, 1e7)
    const controls = new OrbitControls(camera, renderer.domElement)
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8478, 2.2))
    const sun = new THREE.DirectionalLight(0xffffff, 1.6)
    scene.add(sun)
    const material = new THREE.MeshStandardMaterial({ color: 0xc4673f, roughness: 0.55, metalness: 0.05 })

    const load = async (): Promise<THREE.Object3D> => {
      const res = await fetch(url, { credentials: 'include' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const buf = await res.arrayBuffer()
      const ext = name.split('.').pop()?.toLowerCase()
      if (ext === 'stl') return new THREE.Mesh(new STLLoader().parse(buf), material)
      if (ext === '3mf') return new ThreeMFLoader().parse(buf)
      const [{ default: occtFactory }, { default: wasmUrl }] = await Promise.all([import('occt-import-js'), import('occt-import-js/dist/occt-import-js.wasm?url')])
      const occt = await occtFactory({ locateFile: () => wasmUrl })
      const out = occt.ReadStepFile(new Uint8Array(buf), null)
      if (!out.success) throw new Error("couldn't read the STEP file")
      const group = new THREE.Group()
      for (const m of out.meshes) {
        const g = new THREE.BufferGeometry()
        g.setAttribute('position', new THREE.Float32BufferAttribute(m.attributes.position.array, 3))
        if (m.attributes.normal) g.setAttribute('normal', new THREE.Float32BufferAttribute(m.attributes.normal.array, 3))
        else g.computeVertexNormals()
        g.setIndex(m.index.array)
        group.add(new THREE.Mesh(g, m.color ? new THREE.MeshStandardMaterial({ color: new THREE.Color(...m.color), roughness: 0.55 }) : material))
      }
      return group
    }

    load().then((obj) => {
      if (disposed) return
      scene.add(obj)
      const bounds = new THREE.Box3().setFromObject(obj)
      const size = bounds.getSize(new THREE.Vector3()).length() || 1
      const center = bounds.getCenter(new THREE.Vector3())
      controls.target.copy(center)
      camera.position.copy(center).add(new THREE.Vector3(size * 0.7, size * 0.5, size * 0.9))
      sun.position.copy(camera.position)
      camera.near = size / 1000
      camera.far = size * 100
      camera.updateProjectionMatrix()
      setState('ok')
    }, (e: Error) => !disposed && setState(e.message))

    let frame = 0
    const tick = () => { controls.update(); renderer.render(scene, camera); frame = requestAnimationFrame(tick) }
    tick()
    const resize = new ResizeObserver(() => {
      renderer.setSize(el.clientWidth, el.clientHeight)
      camera.aspect = el.clientWidth / el.clientHeight
      camera.updateProjectionMatrix()
    })
    resize.observe(el)
    return () => {
      disposed = true
      cancelAnimationFrame(frame)
      resize.disconnect()
      controls.dispose()
      renderer.dispose()
      el.removeChild(renderer.domElement)
    }
  }, [url, name])

  return (
    <div className="model3d" ref={box}>
      {state === 'loading' && <span className="model3d-note">Loading the model…</span>}
      {state !== 'loading' && state !== 'ok' && <span className="model3d-note error">Couldn't show this model: {state}</span>}
      {state === 'ok' && <span className="model3d-note">Drag to orbit · scroll to zoom</span>}
    </div>
  )
}
