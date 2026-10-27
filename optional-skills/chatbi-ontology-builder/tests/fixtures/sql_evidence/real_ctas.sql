
CREATE  TABLE ttemp.qinpeng_20210104_USER_INFO
DISTRIBUTED BY ('USER_ID')
AS(
   SELECT   T2.DEAL_DATE
           ,T5.CITY_ID      AS REGION_CODE
           ,T1.REGION_DESC
           ,T5.COUNTY_ID    AS CITY_CODE
           ,T3.CITY_DESC
           ,T5.VGRIDDING_CODE -- '网格ID'
           ,T5.VGRIDDING_NAME -- '网格名称'
           ,T2.USER_ID
           ,t2.prod_prcid
           ,T2.INNET_MONS
           ,case when t4.user_id is not null then 1 ELSE 0 end call_flag
   FROM PMID.TB_MID_PAR_USER_MON T2
   LEFT JOIN PMID.TB_MID_PAR_CALL_USER_DAY T4
     ON T2.USER_ID = T4.USER_ID
    AND T4.DEAL_DATE = CAST(SUBSTR('${TxDate}',1,6)||'01' AS DATE)-1 
   LEFT JOIN SXGRID.TB_GRD_USER_DTL_INFO_MON T5
     ON T2.USER_ID = T5.USER_ID
    AND T5.DEAL_DATE=  '$TxMonth'
     LEFT  JOIN PCDE.TB_CDE_LOC_REGION T1
   ON T5.CITY_ID=T1.REGION_CODE
   LEFT JOIN PCDE.TB_CDE_LOC_CITY T3
   ON T5.CITY_ID=T3.REGION_CODE
   AND T5.COUNTY_ID=T3.CITY_CODE
   WHERE T2.DEAL_DATE = '$TxMonth'
   and   T2.user_stat_code<'a'
   and T2.user_type_code<>'02'
   and (    T2.VIRTL_USER_CODE='0' 
         or (T2.VIRTL_USER_CODE<>'0' AND T2.BUSN_BRAND_ID IN ('wt','ww','wu', 'wb', 'nh','nc','nk','np','x1','nv','nt','nw','ns','nu','ny','nm','nd','is')) 
         or (T2.LOC_BRAND_CODE IN ('204') and T2.REGION_CODE='01')
       )
   and not (T2.BUSN_BRAND_ID in('wt','ww','wu','wb','nh','nc','nk','np','x1','nv','nt','nm','nd','nw','ns','nu','ny')
                 or (T2.LOC_BRAND_CODE IN ('204') and T2.REGION_CODE='01')
                 or (T2.BUSN_BRAND_ID='mt' AND T2.PHONE_NO LIKE '1%')) -- 剔固
   and T2.prod_prcid not in (select prod_prcid from pcde.tb_cde_hyk_prodt group by 1)   -- 剔除行业卡
   and not (T2.INNET_DATE=T2.STAT_CHG_DATE AND T2.USER_stat_code in('a','b','c')) -- 剔除两步一快
   and T2.BUSN_BRAND_ID<>'ys' -- 剔除和多号
   and T2.user_id not in (select user_Id 
                          from  (SELECT * FROM PMID.TB_MID_PAR_USER_MON WHERE DEAL_DATE = '${TxMonth}') T
                          where bak_field = 'ttghyz'
                          group by 1) -- 剔除IMS
   and t2.city_code<>'-1'
   group by 1,2,3,4,5,6,7,8,9,10,11
)