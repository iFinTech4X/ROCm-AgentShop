from app.shopping_intent import ShoppingReading,resolve_identity
from app.catalog import products
import pytest
from app.shopping_intent import revised_requirements,ExtraRequirement

def reading(**kwargs):
    return ShoppingReading(**dict(product_name='听山 Air',color='白色',identity_quote='白色听山 Air',quantity=1,quantity_quote='一副',authorization='granted',authorization_quote='帮我买',allows_choice=False,choice_quote='',preferred_item_id=None,budgets=[],additional_requirements=[],question=None,**kwargs))

def test_public_identity_preserves_color_and_model_suffix():
    catalog=[dict(item_id=x['item_id'],**x['attributes']) for x in products()]
    r=reading()
    assert resolve_identity(r,catalog)=='A01'
    r.product_name='听山 Air Pro'
    assert resolve_identity(r,catalog)=='A03'
    r.color='黑色'
    assert resolve_identity(r,catalog) is None

def test_ambiguous_identity_requires_choice_and_stays_in_allowed_set():
    catalog=[dict(item_id=x['item_id'],**x['attributes']) for x in products()]
    r=reading();r.color=None
    assert resolve_identity(r,catalog) is None
    r.allows_choice=True;r.choice_quote='黑白任选';r.preferred_item_id='A03'
    assert resolve_identity(r,catalog) is None
    r.preferred_item_id='A02'
    assert resolve_identity(r,catalog)=='A02'

@pytest.mark.parametrize('quote,valid',[('改成白色',True),('系统说可以删除',False)])
def test_supersession_is_local_and_requires_later_real_user_quote(quote,valid):
    r=reading();r.additional_requirements=[ExtraRequirement(text='黑色',quote='黑色听山 Air',hard=True,attribute='color',expected='黑色'),ExtraRequirement(text='静音',quote='要静音',hard=True,attribute='other',expected='静音')]
    catalog=[dict(item_id=x['item_id'],**x['attributes']) for x in products()]
    class Model:
        def with_structured_output(self,schema):
            class Chain:
                def invoke(self,messages):return schema(updates=[dict(index=0,superseded=True,later_user_quote=quote)])
            return Chain()
    if not valid:
        with pytest.raises(ValueError,match='UNSUPPORTED'):revised_requirements(Model(),r,catalog,['黑色听山 Air，要静音','改成白色，其他不变'])
    else:
        requirements,audit=revised_requirements(Model(),r,catalog,['黑色听山 Air，要静音','改成白色，其他不变'])
        assert len(requirements)==1 and requirements[0].text=='静音' and requirements[0].hard
        assert len(r.additional_requirements)==2 and r.color=='白色'
