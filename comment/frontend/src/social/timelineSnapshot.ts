import type {TimelinePage} from './timelineStream';
// Last complete owner page only, bounded in RAM. Reload drops these bodies.
const pages=new Map<string,TimelinePage>();
export function getTimelineSnapshot(key:string){return pages.get(key);}
export function saveTimelineSnapshot(key:string,page:TimelinePage){
  pages.delete(key);pages.set(key,page);
  while(pages.size>4)pages.delete(pages.keys().next().value!);
}
