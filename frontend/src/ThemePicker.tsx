import { useEffect, useState } from 'react';
type Theme = 'system' | 'dark' | 'light';
export default function ThemePicker() {
  const [theme, setTheme] = useState<Theme>(() => { try { const value = localStorage.getItem('ups-theme'); return value === 'dark' || value === 'light' ? value : 'system'; } catch { return 'system'; } });
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const apply = () => { document.documentElement.dataset.theme = theme === 'system' ? media.matches ? 'dark' : 'light' : theme; window.dispatchEvent(new Event('ups-theme-change')); };
    apply(); media.addEventListener('change', apply);
    try { localStorage.setItem('ups-theme', theme); } catch { /* Preference storage is optional. */ }
    return () => media.removeEventListener('change', apply);
  }, [theme]);
  return <label className="theme-picker"><span>外观</span><select aria-label="外观主题" value={theme} onChange={event => setTheme(event.target.value as Theme)}><option value="system">跟随系统</option><option value="dark">深色</option><option value="light">浅色</option></select></label>;
}
