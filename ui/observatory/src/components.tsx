import {useEffect,useRef} from 'react';
import type {ReactNode} from 'react';
export function Badge({state}:{state:string}){const good=['complete_for_declared_scope','completed','validated','enabled'];const bad=['failed','quarantined','blocked_permission','blocked_auth','blocked_resources'];return <span className={`badge ${good.includes(state)?'good':bad.includes(state)?'warning':''}`}>{state.replaceAll('_',' ')}</span>;}
export function Empty({title,children}:{title:string;children?:ReactNode}){return <div className="empty"><span className="empty-mark" aria-hidden="true">⌁</span><h3>{title}</h3><div>{children}</div></div>;}
export function ErrorBox({message}:{message:string|null}){return message?<div className="error" role="alert">{message}</div>:null;}
export function Loading(){return <div className="loading" role="status"><span/> Reading the selected snapshot…</div>;}
export function Drawer({title,children,onClose}:{title:string;children:ReactNode;onClose:()=>void}){
 const box=useRef<HTMLElement>(null);const prior=useRef<Element|null>(null);
 useEffect(()=>{prior.current=document.activeElement;box.current?.querySelector<HTMLButtonElement>('button')?.focus();const key=(e:KeyboardEvent)=>{if(e.key==='Escape')onClose();};window.addEventListener('keydown',key);return()=>{window.removeEventListener('keydown',key);if(prior.current instanceof HTMLElement)prior.current.focus();};},[onClose]);
 return <aside ref={box} className="drawer" aria-label={title}><div className="drawer-title"><h2>{title}</h2><button aria-label="Close details" onClick={onClose}>×</button></div>{children}</aside>;
}
export function JsonDetails({value,label='Technical record'}:{value:unknown;label?:string}){return <details><summary>{label}</summary><pre>{JSON.stringify(value,null,2)}</pre></details>;}
export function Heading({eyebrow,title,children,actions}:{eyebrow:string;title:string;children:ReactNode;actions?:ReactNode}){return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{children}</p></div>{actions&&<div className="heading-actions">{actions}</div>}</div>;}
