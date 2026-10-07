import { useEffect, useState } from 'react';
import { Coins, Zap } from 'lucide-react';
import { energyLocalDate, energyKwh, energyCoverage, readEnergyUsageDay } from './energyUsageDisplay';
import type { EnergyUsageDay } from './energyUsageDisplay';
import { startVisiblePolling } from './readPolling';

export default function TodayMetrics() {
  const [day, setDay] = useState<EnergyUsageDay | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => startVisiblePolling(async signal => {
    try {
      const next = await readEnergyUsageDay(energyLocalDate(), signal);
      if (!signal.aborted) { setDay(next); setError(false); }
    } catch { if (!signal.aborted) setError(true); }
  }, () => 30000), []);
  const current = !error && day?.date === energyLocalDate() ? day : null;
  return <>
    <div className="metric today-energy"><div className="metric-label"><Zap size={16}/>今日用电 · 估算</div><div className="metric-value">{energyKwh(current?.day.estimate_kwh)}<span>kWh</span></div><p className="muted">{error ? '查询暂不可用' : current ? `已记录 · 覆盖 ${energyCoverage(current.day.coverage_ratio)}${current.capture_fresh ? '' : ' · 采集离线'}` : '正在读取今日记录'}</p></div>
    <div className="metric today-cost"><div className="metric-label"><Coins size={16}/>今日电费 · 估算</div><div className="metric-value">{energyKwh(current?.cost?.estimate_cost, 2)}<span>{current?.cost?.currency === 'CNY' ? '元' : current?.cost?.currency ?? ''}</span></div><p className="muted">{error ? '查询暂不可用' : current?.cost?.rate == null ? '在用电分析中设置电价' : `${current.cost.rate} ${current.cost.currency === 'CNY' ? '元' : current.cost.currency} / kWh · 不补算缺采`}</p></div>
  </>;
}
