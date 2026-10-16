export default function Loading(){
  return <main className="route-loading" role="status" aria-label="Loading page">
    <div className="route-loading-bar"/>
    <div className="route-loading-title"/>
    <div className="route-loading-copy"/>
    <div className="route-loading-cards">{Array.from({length:6},(_,index)=><div key={index}/>)}</div>
  </main>;
}
