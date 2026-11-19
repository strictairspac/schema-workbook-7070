import {searchesApi} from '../api/searches.js';
export const searchesStore={
  items:[],
  async refresh(){this.items=await searchesApi.list();return this.items;},
};
