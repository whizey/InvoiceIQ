export function formatBusinessDate(iso:string):string{
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN",{
    day:"numeric",month:"short",year:"numeric",timeZone:"UTC",
  });
}
