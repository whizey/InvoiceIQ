export type PortalFilters = {q:string;page:number;sort:string};

const sorts=new Set(["posting_desc","posting_asc","due_asc","due_desc","number_asc"]);

export function parsePortalFilters(params:{q?:string;page?:string;sort?:string}):PortalFilters {
  const requested=Number(params.page);
  return {
    q:(params.q||"").slice(0,160),
    page:Number.isSafeInteger(requested)&&requested>0?requested:1,
    sort:sorts.has(params.sort||"")?params.sort!:"posting_desc",
  };
}
