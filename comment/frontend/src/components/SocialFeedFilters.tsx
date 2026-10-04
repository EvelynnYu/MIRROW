import SocialSiteSwitcher from './SocialSiteSwitcher';
import type { SocialSite } from '../pages/SocialRemoteFeed';
import './SocialFeedFilters.css';

export type VisibilityFilter = 'all'|'public'|'private';
export const ALL_SOCIAL_SITES = '__all__';
export default function SocialFeedFilters({sites,current,onSwitch,value,onChange}:{
  sites:SocialSite[]; current:string; onSwitch:(id:string)=>void;
  value:VisibilityFilter; onChange:(value:VisibilityFilter)=>void;
}) {
  return <div className="social-feed-filters">
    <SocialSiteSwitcher sites={sites} current={current} onSwitch={onSwitch} compact />
    <div className="social-feed-filter-segments" role="group" aria-label="动态可见权限筛选">
      {(['all','public','private'] as const).map(filter => <button key={filter} type="button"
        aria-pressed={value===filter} disabled={Boolean(current && current!==ALL_SOCIAL_SITES && filter==='private')}
        onClick={()=>onChange(filter)}>{filter==='all'?'全部':filter==='public'?'公开':'私密'}</button>)}
    </div>
  </div>;
}

export function SocialSourceLabel({name,hosted,onClick}:{name:string;hosted:boolean;onClick?:()=>void}) {
  return onClick ? <button type="button" className="social-source-label" onClick={onClick}
    aria-label={`进入${name}查看${hosted?'寄存':'本家'}动态`}>{name} · {hosted?'寄存':'本家'} ›</button>
    : <span className="social-source-label">{name} · {hosted?'寄存':'本家'}</span>;
}
