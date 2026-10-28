/* 指标飞线画在星云的 WebGL 场景里：世界空间圆管，沿三次曲线落到对应币环的随机节点。
   这里只把脉冲交给场景，不再铺一层会正对相机的二维画布。 */
import { useEffect, useRef } from "react"
import { syncMetricFlights } from "./nebula-scene"

export default function MetricFlights({ pulses, onDone }) {
  const onDoneRef = useRef(onDone)
  onDoneRef.current = onDone

  useEffect(() => {
    let raf = 0
    let stop = false
    const kick = () => {
      if (stop) return
      const handed = syncMetricFlights(pulses, (id) => onDoneRef.current?.(id))
      if (!handed) raf = requestAnimationFrame(kick)
    }
    kick()
    return () => {
      stop = true
      cancelAnimationFrame(raf)
    }
  }, [pulses])

  return null
}
