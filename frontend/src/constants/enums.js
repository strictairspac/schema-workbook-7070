export const ROLES={admin:'管理员',operator:'操作员',viewer:'观察员'};
export const RUN_STATES={queued:'排队中',running:'执行中',completed:'已完成',failed:'失败'};
// 受限分表求解状态：页面需明确区分待续跑、已最优与无解。
export const SEARCH_STATES={
  queued:'排队中',
  running:'执行中',
  waiting:'待续跑',
  optimal:'已最优',
  infeasible:'无解',
  failed:'失败',
};
