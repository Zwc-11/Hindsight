import {useEffect,useRef,useState} from 'react';
import type {Scope} from './types';
export function scopeQuery(scope:Scope, extra:Record<string,string|number|undefined>={}):string {
  const q=new URLSearchParams({snapshot:String(scope.snapshot),as_of:new Date(scope.as_of).toISOString(),mode:scope.mode,purpose:scope.purpose});
  for(const [k,v] of Object.entries(extra)) if(v!==undefined&&v!=='')q.set(k,String(v));
  return q.toString();
}
export async function request<T>(path:string, options:RequestInit={}):Promise<T>{
  const r=await fetch(path,{...options,headers:{'Content-Type':'application/json',...options.headers}});
  if(!r.ok){const body=await r.text();let reason=body;try{const parsed=JSON.parse(body) as {error?:string};reason=parsed.error??body;}catch{/* Non-JSON error from a local proxy. */}throw new Error(`${r.status} · ${reason.slice(0,500)}`);}
  return r.json() as Promise<T>;
}
export function mutate<T>(path:string,token:string,payload:unknown):Promise<T>{return request<T>(path,{method:'POST',headers:{'X-Hindsight-Token':token},body:JSON.stringify(payload)});}
export function useData<T>(path:string|null, refresh=0){
  const [data,setData]=useState<T|null>(null);const[error,setError]=useState<string|null>(null);const[loading,setLoading]=useState(false);const generation=useRef(0);
  useEffect(()=>{const id=++generation.current;const abort=new AbortController();setData(null);setError(null);setLoading(Boolean(path));
    if(path)request<T>(path,{signal:abort.signal}).then(v=>{if(id===generation.current&&!abort.signal.aborted)setData(v);}).catch(e=>{if(!abort.signal.aborted&&id===generation.current)setError(e instanceof Error?e.message:'Request failed');}).finally(()=>{if(id===generation.current&&!abort.signal.aborted)setLoading(false);});
    return()=>abort.abort();
  },[path,refresh]);
  return{data,error,loading};
}
export const number=(v:number|null|undefined,digits=2)=>v==null?'—':new Intl.NumberFormat('en-CA',{maximumFractionDigits:digits}).format(v);
export const bytes=(n:number)=>n>=2**30?`${number(n/2**30,2)} GiB`:n>=2**20?`${number(n/2**20,1)} MiB`:`${number(n/2**10,1)} KiB`;
export const date=(v:number|null|undefined)=>v==null?'Not supplied':new Date(v).toISOString().replace('T',' ').slice(0,19)+' UTC';
