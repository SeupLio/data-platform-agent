# 数据模型说明书（原始 wiki 摘录）

> 非结构化原始语料。由 `dataagent.knowledge.build` 拆解为 `data/built/models.yaml`。
> Agent 做「查询可下钻到哪些维度」「这个指标能配哪些筛选」时读的就是拆解后的这份。

## dwd_video_play_detail

- 分层：DWD
- 业务域：消费
- 粒度：用户 × 稿件 × 单次播放事件
- 分区字段：date
- 字段：date, user_id, video_id, up_id, category, platform, play_cnt, duration_sec, is_finish, is_valid_play
- 上游：ods_client_log_play
- 下游：dws_video_daily
- 负责人：数据平台-消费数据组
- 备注：明细层，禁止直接对报表开放，日增约 8 亿行

## dws_video_daily

- 分层：DWS
- 业务域：消费
- 粒度：日期 × 分区 × 端
- 分区字段：date
- 字段：date, category, platform, upload_cnt, audit_pass_cnt, play_cnt, watch_uv, finish_cnt, valid_play_cnt, duration_sec, like_cnt, coin_cnt, fav_cnt, share_cnt, comment_cnt, danmu_cnt, triple_cnt
- 上游：dwd_video_play_detail, dwd_video_audit
- 下游：ads_consumption_overview, ads_category_rank
- 负责人：数据平台-消费数据组
- 备注：消费域主表，指标平台的默认取数表

## dws_user_daily

- 分层：DWS
- 业务域：用户增长
- 粒度：日期 × 端
- 分区字段：date
- 字段：date, platform, dau, new_user_cnt, retain_1d_cnt, retain_7d_cnt, usage_sec, pay_user_cnt, pay_amount, vip_open_cnt, ad_income, ad_impression, ad_click
- 上游：dwd_user_active, dwd_order_pay
- 下游：ads_growth_overview
- 负责人：数据平台-增长数据组
- 备注：增长与商业化共用，付费相关字段需脱敏后对外

## dws_up_daily

- 分层：DWS
- 业务域：创作
- 粒度：日期 × UP主 × 分区
- 分区字段：date
- 字段：date, up_id, category, publish_cnt, play_cnt, follower_add_cnt, income
- 上游：dwd_video_audit, dwd_up_follow
- 下游：ads_creator_overview
- 负责人：数据平台-创作数据组
- 备注：up_id 为敏感字段，对外必须脱敏

## ads_consumption_overview

- 分层：ADS
- 业务域：消费
- 粒度：日期 × 分区
- 分区字段：date
- 字段：date, category, play_cnt, watch_uv, finish_rate, avg_play_duration
- 上游：dws_video_daily
- 下游：BI 看板-消费总览
- 负责人：数据平台-消费数据组
- 备注：应用层，禁止被其它 ADS 表直接引用

## dim_category

- 分层：DIM
- 业务域：公共
- 粒度：分区
- 分区字段：无
- 字段：category, category_name, parent_category, is_core
- 上游：ods_category_meta
- 下游：dws_video_daily, ads_consumption_overview
- 负责人：数据平台-公共数据组
- 备注：维表，无需分区过滤
