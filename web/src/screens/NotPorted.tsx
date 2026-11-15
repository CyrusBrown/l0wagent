/** Placeholder for screens the mockup has but this app doesn't yet. */
export function NotPorted({ name, milestone }: { name: string; milestone?: string }) {
  return (
    <div className="page narrow">
      <h1>{name}</h1>
      <div className="empty">
        {milestone ? <>Not built yet: arrives with {milestone}. The design is in <code>design/mockup/</code>.</> : 'There is nothing at this address.'}
      </div>
    </div>
  )
}
