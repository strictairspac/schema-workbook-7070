import {api,downloadUrl} from './client.js';
export const searchesApi={
  list:()=>api('/searches'),
  create:data=>api('/searches','POST',data),
  detail:id=>api('/searches/'+id),
  resume:id=>api('/searches/'+id+'/resume','POST'),
  exportReport:id=>downloadUrl('/searches/'+id+'/export','search-'+id+'.json'),
};
