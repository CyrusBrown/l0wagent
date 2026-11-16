declare module 'occt-import-js' {
  type Mesh = { attributes: { position: { array: number[] }; normal?: { array: number[] } }; index: { array: number[] }; color?: [number, number, number] }
  type Occt = { ReadStepFile(content: Uint8Array, params: unknown): { success: boolean; meshes: Mesh[] } }
  const factory: (opts?: { locateFile?: (name: string) => string }) => Promise<Occt>
  export default factory
}
