import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'

export type TimeFormat = '24h' | '12h'
const KEY = 'l0w.timeFormat'

function read(): TimeFormat {
  try {
    return localStorage.getItem(KEY) === '12h' ? '12h' : '24h'
  } catch {
    return '24h'
  }
}

let current: TimeFormat = read()

export function getTimeFormat(): TimeFormat {
  return current
}

export function setTimeFormat(format: TimeFormat) {
  current = format
}

export interface TimeFormatValue {
  format: TimeFormat
  hour12: boolean
  setFormat: (format: TimeFormat) => void
}

const defaultContext: TimeFormatValue = { format: current, hour12: current === '12h', setFormat: setTimeFormat }
const TimeFormatContext = createContext<TimeFormatValue>(defaultContext)

export function TimeFormatProvider({ children }: { children: ReactNode }) {
  const [format, setFormatState] = useState<TimeFormat>(read)
  setTimeFormat(format)
  useEffect(() => {
    try { localStorage.setItem(KEY, format) } catch { /* private mode */ }
  }, [format])
  const setFormat = useCallback((next: TimeFormat) => setFormatState(next), [])
  return <TimeFormatContext.Provider value={{ format, hour12: format === '12h', setFormat }}>{children}</TimeFormatContext.Provider>
}

export function useTimeFormat(): TimeFormatValue {
  return useContext(TimeFormatContext)
}
