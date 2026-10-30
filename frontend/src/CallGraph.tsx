import React,{useState,useEffect,useMemo,useRef,useId} from 'react';
import {Maximize2,X,GitBranch,CornerUpLeft,ArrowUpRight} from 'lucide-react';
import {buildCallGraph,edgePath} from './callGraphModel';
import type {TraceEvent,CallGraphModel,GraphNode} from './callGraphModel';
import './callGraph.css';
const states={returned:'已返回',running:'处理中',interrupted:'未完成',error:'曾异常'};
const kinds={route:'协调分派',nested:'嵌套调用',return:'返回流转',sequence:'阶段衔接'};

function GraphCanvas({graph,selected,onSelect,returns}:{graph:CallGraphModel;selected:string|null;onSelect:(id:string)=>void;returns:boolean}){
 const uid=useId().replace(/:/g,'');
 return <svg className="collaboration-canvas" viewBox={`0 0 ${graph.width} ${graph.height}`} role="group" aria-label="按角色分层的实际协作图">
  <defs>{['route','nested','return','sequence'].map(kind=><marker key={kind} id={`${uid}-${kind}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" className={`edge-arrow ${kind}`}/></marker>)}</defs>
  {graph.edges.filter(e=>returns||e.kind!=='return').map(e=>{const a=graph.nodes.find(n=>n.id===e.from)!,b=graph.nodes.find(n=>n.id===e.to)!;return <path key={e.id} d={edgePath(a,b,e.kind)} markerEnd={`url(#${uid}-${e.kind})`} className={`collaboration-edge ${e.kind} ${selected&&![e.from,e.to].includes(selected)?'muted':''}`} data-edge-from={e.from} data-edge-to={e.to} data-edge-kind={e.kind}><title>{a.name} → {b.name} · {kinds[e.kind]} {e.count} 次 · 记录 {e.evidence.join(', ')}</title></path>;})}
  {graph.nodes.map(n=><g key={n.id} transform={`translate(${n.x} ${n.y})`} className={`collaboration-node ${n.status} ${selected===n.id?'selected':''}`} role="button" tabIndex={0} aria-pressed={selected===n.id} aria-label={`${n.name}，${n.invocations.length}次进入，模型返回${n.models}次，${states[n.status]}`} onClick={()=>onSelect(n.id)} onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();onSelect(n.id);}}} data-role-key={n.id}>
   <rect width={n.width} height={n.height} rx="9"/><circle cx="13" cy="15" r="3"/>
   <text x="24" y="19" className="node-hint">{n.hint}</text><text x="13" y="43" className="node-title">{n.shortName}</text><text x="13" y="63" className="node-count">{n.invocations.length} 次进入 · {n.models} 次模型返回</text>
  </g>)}
 </svg>;
}
function NodeDetails({graph,node,onClose}:{graph:CallGraphModel;node:GraphNode;onClose:()=>void}){
 const [invocation,setInvocation]=useState(node.invocations.at(-1)!.id);
 useEffect(()=>setInvocation(node.invocations.at(-1)!.id),[node.id,graph.runId]);
 const current=node.invocations.find(n=>n.id===invocation)||node.invocations.at(-1)!,edges=graph.edges.filter(e=>e.from===node.id||e.to===node.id);
 return <section className="graph-inspector" aria-label="节点真实调用记录">
  <header><div><span className="graph-eyebrow">实际调用记录</span><h4>{node.name}</h4></div><button aria-label="关闭节点详情" onClick={onClose}><X size={17}/></button></header>
  <div className="graph-stats"><span>{node.invocations.length} 次进入</span><span>{node.models} 次模型返回</span><span>{states[node.status]}</span></div>
  <label className="invocation-picker">查看某次调用<select value={current.id} onChange={e=>setInvocation(e.target.value)}>{node.invocations.map((n,i)=><option key={n.id} value={n.id}>第 {i+1} 次 · {new Date(n.start.created).toLocaleTimeString('zh-CN',{hour12:false})} · {n.failed?'异常':n.ended?'已返回':'未结束'}</option>)}</select></label>
  <div className="invocation-meta">{current.start.payload.node_kind==='module'?'程序模块':'模型角色'} · 记录 #{current.start.id} · {current.models} 次模型返回</div>
  <div className="invocation-events">{current.events.filter(e=>!['agent_start','agent_end'].includes(e.kind)).map(e=><details key={e.id}><summary><span>{e.title}</span><small>#{e.id}</small></summary><pre>{JSON.stringify(e.payload,null,2)}</pre></details>)}{!current.events.some(e=>!['agent_start','agent_end'].includes(e.kind))&&<p>该次调用尚无返回记录。</p>}</div>
  {!!edges.length&&<details className="relation-evidence"><summary>与其他角色的关系 <span>{edges.length}</span></summary>{edges.map(e=><div key={e.id}><b>{graph.nodes.find(n=>n.id===e.from)?.shortName} → {graph.nodes.find(n=>n.id===e.to)?.shortName}</b><small>{kinds[e.kind]} {e.count} 次 · 记录 {e.evidence.join(', ')}</small></div>)}</details>}
 </section>;
}
export function CallGraph({events,active}:{events:TraceEvent[];active:boolean}){
 const graph=useMemo(()=>buildCallGraph(events,active),[events,active]);
 const [selected,setSelected]=useState<string|null>(null),[returns,setReturns]=useState(false),[expanded,setExpanded]=useState(false),[zoom,setZoom]=useState(false),dialog=useRef<HTMLDialogElement>(null);
 useEffect(()=>setSelected(null),[graph.runId]);
 useEffect(()=>{if(expanded&&!dialog.current?.open)dialog.current?.showModal();if(!expanded&&dialog.current?.open)dialog.current.close();},[expanded]);
 const node=graph.nodes.find(n=>n.id===selected),pick=(id:string)=>setSelected(selected===id?null:id);
 const controls=<button className={`return-toggle ${returns?'on':''}`} aria-pressed={returns} onClick={()=>setReturns(!returns)}><CornerUpLeft size={13}/>{returns?'隐藏':'显示'}返回流</button>;
 return <section className="collaboration-graph" aria-label="真实智能体协作调用图">
  <div className="collaboration-heading"><div><GitBranch size={16}/><b>协作调用图</b></div><button onClick={()=>setExpanded(true)} aria-label="展开协作调用图"><Maximize2 size={15}/></button></div>
  <p className="graph-description">按角色展示分工与交接，点击查看每次调用。</p>
  <div className="graph-toolbar"><span>{graph.nodes.length} 个角色 · {graph.invocations.length} 次进入</span>{controls}</div>
  {graph.nodes.length?<><GraphCanvas graph={graph} selected={selected} onSelect={pick} returns={returns}/><div className="graph-legend"><span><i/>分派</span><span><i className="nested"/>嵌套调用</span><span><i className="sequence"/>阶段衔接</span>{returns&&<span><i className="return"/>返回</span>}</div><p className="graph-footnote">仅显示已运行角色；分支不表示并行，返回记录可展开。</p>{node&&<NodeDetails graph={graph} node={node} onClose={()=>setSelected(null)}/>}</>:<p className="graph-empty">{events.length?'当前记录还没有可绘图的调用编号，下方保留原始事件。':'等待首次实际调用，运行后自动形成协作图。'}</p>}
  <dialog className="graph-dialog" ref={dialog} onCancel={()=>setExpanded(false)} onClose={()=>setExpanded(false)} aria-labelledby="expanded-graph-title">
   <header><div><span className="graph-eyebrow">TAPay · 实际执行关系</span><h3 id="expanded-graph-title">协作调用图</h3><p>分层查看角色分工，重复调用收在同一节点内。</p></div><button onClick={()=>setExpanded(false)} aria-label="关闭展开调用图"><X size={22}/></button></header>
   <div className="expanded-graph-toolbar"><span>{graph.nodes.length} 个角色 · {graph.invocations.length} 次进入 · {graph.invocations.reduce((sum,n)=>sum+n.models,0)} 次模型返回</span><div className="graph-view-controls"><button className="return-toggle" onClick={()=>setZoom(!zoom)}>{zoom?'适应窗口':'放大查看'}</button>{controls}</div></div>
   <div className="expanded-graph-body"><div className={`expanded-graph-canvas ${zoom?'zoomed':'fit'}`}>{graph.nodes.length?<GraphCanvas graph={graph} selected={selected} onSelect={pick} returns={returns}/>:<p>尚无实际调用记录。</p>}<p className="graph-footnote">连线来自协调分派、嵌套调用或记录中的阶段衔接。角色返回可能形成回路；这里使用分层布局，不代表并行执行。</p></div>{node?<NodeDetails graph={graph} node={node} onClose={()=>setSelected(null)}/>:<div className="graph-inspector-empty"><GitBranch size={28}/><h4>点击一个角色</h4><p>查看每次进入、工具与核验记录，<br/>以及连线所依据的原始事件。</p><span><ArrowUpRight size={15}/>记录随任务实时更新</span></div>}</div>
  </dialog>
 </section>;
}
