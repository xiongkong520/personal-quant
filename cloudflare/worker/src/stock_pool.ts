/**
 * 全市场搜索的基础股票池（代码 / 名称 / 拼音首字母）。
 * 与本地 `src/quant/api/server.py` 的 STOCK_POOL、`scripts/push_stock_pool.py` 的 40 只保持一致；
 * D1 stock_names 表里若有更多名字，搜索会合并使用（这里只补名称与拼音首字母）。
 */
export interface PoolStock {
  symbol: string;
  name: string;
  initials: string;
}

export const STOCK_POOL: PoolStock[] = [
  { symbol: "600519", name: "贵州茅台", initials: "GZMT" },
  { symbol: "000001", name: "平安银行", initials: "PAYH" },
  { symbol: "300750", name: "宁德时代", initials: "NDSD" },
  { symbol: "601318", name: "中国平安", initials: "ZGPA" },
  { symbol: "600036", name: "招商银行", initials: "ZSYH" },
  { symbol: "000858", name: "五粮液", initials: "WLY" },
  { symbol: "002594", name: "比亚迪", initials: "BYD" },
  { symbol: "601899", name: "紫金矿业", initials: "ZJKY" },
  { symbol: "600900", name: "长江电力", initials: "CJDL" },
  { symbol: "601088", name: "中国神华", initials: "ZGSH" },
  { symbol: "600030", name: "中信证券", initials: "ZXZQ" },
  { symbol: "000651", name: "格力电器", initials: "GLDQ" },
  { symbol: "000333", name: "美的集团", initials: "MDJT" },
  { symbol: "600276", name: "恒瑞医药", initials: "HRYY" },
  { symbol: "300059", name: "东方财富", initials: "DFCF" },
  { symbol: "002415", name: "海康威视", initials: "HKWS" },
  { symbol: "600887", name: "伊利股份", initials: "YLGF" },
  { symbol: "601012", name: "隆基绿能", initials: "LJLN" },
  { symbol: "600585", name: "海螺水泥", initials: "HLSN" },
  { symbol: "601166", name: "兴业银行", initials: "XYYH" },
  { symbol: "000725", name: "京东方A", initials: "JDFA" },
  { symbol: "601398", name: "工商银行", initials: "GSYH" },
  { symbol: "601857", name: "中国石油", initials: "ZGSY" },
  { symbol: "600028", name: "中国石化", initials: "ZGSH" },
  { symbol: "601988", name: "中国银行", initials: "ZGYH" },
  { symbol: "601601", name: "中国太保", initials: "ZGTB" },
  { symbol: "601628", name: "中国人寿", initials: "ZGRS" },
  { symbol: "600016", name: "民生银行", initials: "MSYH" },
  { symbol: "600104", name: "上汽集团", initials: "SQJT" },
  { symbol: "601668", name: "中国建筑", initials: "ZGJZ" },
  { symbol: "000002", name: "万科A", initials: "WKA" },
  { symbol: "000063", name: "中兴通讯", initials: "ZXTX" },
  { symbol: "600009", name: "上海机场", initials: "SHJC" },
  { symbol: "601888", name: "中国中免", initials: "ZGZM" },
  { symbol: "603259", name: "药明康德", initials: "YMKD" },
  { symbol: "688981", name: "中芯国际", initials: "ZXGJ" },
  { symbol: "688111", name: "金山办公", initials: "JSBG" },
  { symbol: "300760", name: "迈瑞医疗", initials: "MRYL" },
  { symbol: "002475", name: "立讯精密", initials: "LXJM" },
  { symbol: "002352", name: "顺丰控股", initials: "SFKG" },
];

/** 指数代码 → 展示名。 */
export const INDEX_NAMES: Record<string, string> = {
  sh000001: "上证指数",
  sz399001: "深证成指",
  sz399006: "创业板指",
  sh000300: "沪深300",
};
