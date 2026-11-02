# JD review summary API contract

## Current product-page API

- Page: `https://item.jd.com/<SPU>.html`
- Trigger: product page initial load
- Method: `GET`
- Host: `https://api.m.jd.com/`
- `functionId`: `getLegoWareDetailComment`
- Request body:

```json
{"shopType":"0","sku":"<SPU>","commentNum":5,"source":"pc"}
```

- Browser-managed inputs: `h5st`, `x-api-eid-token`, `uuid`, cookies
- Response object: find the nested object containing both `allCntStr` and `goodRate`
- Field mapping:
  - 页面“买家评价”数量 → `allCntStr`
  - `goodRate` 返回百分比；页面完整展示文案按京东模板为 `超{goodRate}买家赞不绝口`

The JD page creates `window.PSign` with app id `fb5df`. Its current frontend wrapper signs a SHA-256 hash of the request body and sends `appid=item-v3`, `client=pc`, `clientVersion=1.0.0`, `loginType=3`, and `build=100000`. Always call the page-owned signer in the page context. Do not reimplement, reverse, log, or persist the signature.

## Batch fast path

- Method: `GET`
- Endpoint: `https://club.jd.com/comment/productCommentSummaries.action`
- Query: `referenceIds=<comma-separated SPUs>`
- Response list: `CommentsCount`
- Field mapping:
  - `CommentCountStr` → `评价数`
  - `GoodRateShow` → 按京东模板生成 `超{GoodRateShow}%买家赞不绝口`

该接口作为每批 20 个 SPU 的快速路径；失败或缺失项交给当前签名接口补偿。A response containing `系统繁忙`, HTML, an empty list, or an unmatched SKU is not success.

## UI normalization

| API value | Stored value |
|-|-|
| `allCntStr = "10万+"` | `评价数 = "10万+"` |
| `goodRate = "99%"` | `好评率 = "超99%买家赞不绝口"` |
| `GoodRateShow = 99` | `好评率 = "超99%买家赞不绝口"` |

Never convert `10万+` to an exact count. Never write zero for missing or blocked data.
