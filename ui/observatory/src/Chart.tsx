import {useEffect,useRef} from 'react';
import * as echarts from 'echarts/core';
import {LineChart,BarChart,ScatterChart} from 'echarts/charts';
import {GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,AriaComponent} from 'echarts/components';
import {CanvasRenderer} from 'echarts/renderers';
import type {EChartsCoreOption} from 'echarts/core';
echarts.use([LineChart,BarChart,ScatterChart,GridComponent,TooltipComponent,LegendComponent,DataZoomComponent,AriaComponent,CanvasRenderer]);
export function Chart({labels,lines,dark,title,height=280,onSelect}:{labels:string[];lines:{name:string;values:(number|null)[]}[];dark:boolean;title:string;height?:number;onSelect?:(index:number)=>void}){
  const ref=useRef<HTMLDivElement>(null);
  useEffect(()=>{if(!ref.current)return;const el=ref.current;const chart=echarts.init(el);const ink=dark?'#d7dbdf':'#383e42';const grid=dark?'#30373c':'#e3e5e4';
    const options:EChartsCoreOption={animation:!window.matchMedia('(prefers-reduced-motion: reduce)').matches,animationDuration:180,backgroundColor:'transparent',color:dark?['#8fb5ed','#d2b787','#86b9ae']:['#245b96','#906724','#306e62'],textStyle:{fontFamily:'inherit',color:ink},aria:{enabled:true,label:{description:title}},grid:{left:68,right:20,top:35,bottom:46},tooltip:{trigger:'axis',renderMode:'richText',confine:true},legend:{top:0,textStyle:{color:ink}},xAxis:{type:'category',data:labels,boundaryGap:false,axisLabel:{color:ink,hideOverlap:true},axisLine:{lineStyle:{color:grid}},axisTick:{show:false}},yAxis:{type:'value',scale:true,axisLabel:{color:ink},splitLine:{lineStyle:{color:grid}}},series:lines.map(line=>({name:line.name,type:'line',data:line.values,showSymbol:labels.length<60,symbolSize:5,connectNulls:false,lineStyle:{width:1.7},emphasis:{focus:'series'}}))};
    chart.setOption(options);if(onSelect)chart.on('click',p=>{if(typeof p.dataIndex==='number')onSelect(p.dataIndex);});
    const observer=new ResizeObserver(()=>chart.resize());observer.observe(el);return()=>{observer.disconnect();chart.dispose();};
  },[labels,lines,dark,title,onSelect]);
  return <div ref={ref} className="chart" style={{height}} role="img" aria-label={title}/>;
}
