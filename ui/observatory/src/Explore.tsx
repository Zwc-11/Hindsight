import {useState,useMemo,useCallback} from 'react';
import type {Scope,Series,SeriesResponse,Point} from './types';
import {useData,scopeQuery,number,date} from './api';
import {Chart} from './Chart';
import {Empty,ErrorBox,Loading,JsonDetails} from './components';
export type ExploreProps={scope:Scope;search:string;dark:boolean;selected:string|null;select:(id:string)=>void;compare:(id:string)=>void;onEvidence:(id:string)=>void};
export function Explore({scope,search,dark,selected,select,compare,onEvidence}:ExploreProps){
 const[provider,setProvider]=useState('');const[after,setAfter]=useState('');const[stack,setStack]=useState<string[]>([]);const[start,setStart]=useState('1960-01-01');const[end,setEnd]=useState(new Date().toISOString().slice(0,10));const[indexed,setIndexed]=useState(false);
 const catalog=useData<{rows:Series[];next:string|null}>(`/api/catalog?${scopeQuery(scope,{search,provider,after,limit:50})}`);
 const detail=useData<SeriesResponse>(selected?`/api/series/${encodeURIComponent(selected)}?${scopeQuery(scope,{start,end,limit:2000})}`:null);
 const points=detail.data?.points??[];const base=points.find(p=>p.value!==null&&p.value!==0)?.value;
 const labels=useMemo(()=>points.map(p=>p.period_end.slice(0,10)),[points]);
 const lines=useMemo(()=>[{name:indexed?'Indexed to 100':detail.data?.series.name??'Observation',values:points.map(p=>p.value===null?null:indexed&&base?p.value/base*100:p.value)}],[points,indexed,base,detail.data?.series.name]);
 const clicked=useCallback((i:number)=>{const p=points[i];if(p)onEvidence(p.id);},[points,onEvidence]);
 const reset=()=>{setAfter('');setStack([]);};
 return <div className="explorer-layout">
  <section className="catalog-pane" aria-label="Series catalog">
   <div className="section-top"><h2>Series catalog</h2><label className="small-label">Provider<select value={provider} onChange={e=>{setProvider(e.target.value);reset();}}><option value="">All providers</option>{['worldbank','issuer','census','bls','alpaca','sec','eia','bea'].map(p=><option key={p}>{p}</option>)}</select></label></div>
   <ErrorBox message={catalog.error}/>{catalog.loading&&<Loading/>}
   <div className="series-list">{catalog.data?.rows.map(s=><button key={s.id} className={`series-row ${selected===s.id?'selected':''}`} onClick={()=>select(s.id)}><span className="series-title">{s.name||s.code}</span><span className="series-meta">{s.entity} <span>·</span> {s.frequency}</span><span className="series-bottom"><code>{s.code}</code><span>{s.captured_version_count?number(s.captured_version_count,0)+' records':'Metadata'}</span></span></button>)}</div>
   {catalog.data?.rows.length===0&&<Empty title="No matching series">Change the search/provider filter, or inspect source access and queued catalog work.</Empty>}
   <div className="pagination"><button disabled={!stack.length} onClick={()=>{const prev=stack.at(-1)??'';setStack(stack.slice(0,-1));setAfter(prev);}}>Previous</button><span>Up to 50 entries</span><button disabled={!catalog.data?.next} onClick={()=>{setStack([...stack,after]);setAfter(catalog.data?.next??'');}}>Next</button></div>
  </section>
  <section className="series-detail" aria-label="Selected series">
   {!selected&&<Empty title="Choose an observation series">Explore actual provider data. Metadata-only entries remain visible so missing histories cannot disappear.</Empty>}
   <ErrorBox message={detail.error}/>{detail.loading&&<Loading/>}
   {detail.data&&<><div className="series-heading"><div className="eyebrow">{detail.data.series.provider} / {detail.data.series.entity}</div><h2>{detail.data.series.name}</h2><p>{detail.data.series.description||'Read the source methodology before interpreting the measure.'}</p><div className="inline-meta"><span>{detail.data.series.unit}</span><span>{detail.data.series.frequency}</span><button onClick={()=>compare(detail.data!.series.id)}>Add to comparison</button></div></div>
    <div className="range-controls"><label>From<input type="date" value={start} onChange={e=>setStart(e.target.value)}/></label><label>Through<input type="date" value={end} onChange={e=>setEnd(e.target.value)}/></label><label className="check-label"><input type="checkbox" checked={indexed} onChange={e=>setIndexed(e.target.checked)}/> Index first nonzero observation to 100</label></div>
    {points.length>0?<><Chart labels={labels} lines={lines} dark={dark} title={`${detail.data.series.name}; ${detail.data.series.unit}; gaps retained`} onSelect={clicked}/><div className="chart-caption">{number(points.length,0)} captured periods · {number(points.filter(p=>p.value===null).length,0)} source nulls · {indexed?'Display transform only; analytical inputs unchanged.':'Native units.'} Click a point for evidence.</div></>:<Empty title="History is not available in this view">{scope.mode==='reconstructed'?'No verified publication-time vintage is eligible. Current captured data is not substituted.':'This entry is metadata-only, queued, or blocked. Check Sources & jobs.'}</Empty>}
    {detail.data.truncated&&<div className="notice">Response reached the 2,000-record limit. Narrow the date range; this chart is not the entire history.</div>}
    <div className="notice subtle">{detail.data.warning}</div>
    <details className="data-table-details" open={points.length<10}><summary>Observation table and source evidence</summary><div className="table-scroll" tabIndex={0} aria-label="Scrollable data table"><table><thead><tr><th>Period</th><th className="numeric">Value</th><th>Unit</th><th>Publication</th><th>Evidence</th></tr></thead><tbody>{points.slice(-150).reverse().map((p:Point)=><tr key={p.id}><td>{p.period_start} — {p.period_end}</td><td className="numeric">{number(p.value,5)}</td><td>{p.unit}</td><td>{date(p.published_at)}</td><td><button className="link-button" onClick={()=>onEvidence(p.id)}>Inspect</button></td></tr>)}</tbody></table></div>{points.length>150&&<p className="muted">Showing the latest 150 of the returned observations.</p>}</details>
    {detail.data.series.source_url&&<a className="source-link" href={detail.data.series.source_url} target="_blank" rel="noreferrer">Open original source ↗</a>}
    <JsonDetails value={detail.data.series.metadata} label="Measurement definition and source metadata"/>
   </>}
  </section>
 </div>;
}
