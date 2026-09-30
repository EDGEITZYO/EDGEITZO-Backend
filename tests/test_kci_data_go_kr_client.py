"""공공데이터포털 KCI 논문정보서비스 응답 파싱 (app/integrations/kci/data_go_kr_client.py)."""
import pytest

from app.integrations.kci.data_go_kr_client import DataGoKrError, QuotaExceeded, parse_page

NORMAL = """<response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE</resultMsg></header>
<body><items><item><NUM>1</NUM><ARTICRETID>00000000000013016459</ARTICRETID><ARTIID>ART002780520</ARTIID>
<CRETDIVCD>01</CRETDIVCD><CRETID>CRT001935014</CRETID><BELOINSINM>제주대학교 아열대원예산업연구소</BELOINSINM><ORCID/></item>
</items><recordCnt>100</recordCnt><pageNo>1</pageNo><totalCount>8</totalCount></body></response>"""

GATEWAY_QUOTA = """<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR</returnAuthMsg>
<returnReasonCode>22</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""

GATEWAY_KEY = """<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg>
<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>
<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"""


def test_정상_응답에서_저자_번호를_꺼낸다():
    page = parse_page(NORMAL)
    assert page.total == 8
    assert page.items[0]["CRETID"] == "CRT001935014"
    assert page.items[0]["ORCID"] == ""


def test_하루_한도_초과는_따로_알린다():
    # 수집기가 이걸 보고 그날 호출을 멈춘다
    with pytest.raises(QuotaExceeded):
        parse_page(GATEWAY_QUOTA)


def test_그_밖의_게이트웨이_오류():
    with pytest.raises(DataGoKrError):
        parse_page(GATEWAY_KEY)


@pytest.mark.asyncio
async def test_오류_메시지에_인증키가_남지_않는다():
    import httpx

    from app.integrations.kci.data_go_kr_client import KciDataGoKrClient

    def boom(request):
        raise httpx.ReadTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        client = KciDataGoKrClient(http, key="SECRET-KEY-123")
        with pytest.raises(Exception) as info:
            await client.authors_by_name("김민정")
    assert "SECRET-KEY-123" not in str(info.value)


@pytest.mark.asyncio
async def test_HTTP_오류도_키_없이():
    import httpx

    from app.integrations.kci.data_go_kr_client import KciDataGoKrClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(504))) as http:
        client = KciDataGoKrClient(http, key="SECRET-KEY-123")
        with pytest.raises(Exception) as info:
            await client.article_authors("ART1")
    assert "SECRET-KEY-123" not in str(info.value) and "504" in str(info.value)
