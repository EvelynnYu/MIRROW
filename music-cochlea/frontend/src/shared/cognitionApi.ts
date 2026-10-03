import { getApiBase, getAccessHeaders } from './config';
export async function cognitionRequest(path: string, data?: unknown) {
  const response=await fetch(getApiBase()+'/api/cognition'+path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json',...getAccessHeaders()},body:data?JSON.stringify(data):undefined});
  if(!response.ok) throw new Error('音乐体验记录操作未完成');
  return response.json();
}
