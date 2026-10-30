export function formatInrCompact(value:number|string):string{
  const amount=Number(value);
  if(!Number.isFinite(amount))return "Unavailable";
  const absolute=Math.abs(amount);
  const [scale,suffix]=absolute>=1e7?[1e7,"Cr"]:absolute>=1e5?[1e5,"L"]:absolute>=1e3?[1e3,"k"]:[1,""];
  const display=new Intl.NumberFormat("en-IN",{maximumFractionDigits:2}).format(absolute/Number(scale));
  return `${amount<0?"−":""}₹${display}${suffix}`;
}
