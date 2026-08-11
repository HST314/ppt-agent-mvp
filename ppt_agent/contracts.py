from __future__ import annotations

import re

from .errors import ValidationError


def validate_instance(value, schema, root=None):
    """Validate the JSON-Schema subset emitted and published by P1 contracts."""
    root = root or schema
    if "$ref" in schema:
        target=root
        for part in schema["$ref"].removeprefix("#/").split("/"):
            target=target[part]
        return validate_instance(value,target,root)
    if "const" in schema and value != schema["const"]: raise ValidationError("实例不符合 const")
    if "enum" in schema and value not in schema["enum"]: raise ValidationError("实例不符合 enum")
    expected=schema.get("type")
    if isinstance(expected,list):
        if value is None and "null" in expected: return value
        expected=next((x for x in expected if x != "null"),None)
    types={"object":dict,"array":(list,tuple),"string":str,"integer":int,"boolean":bool}
    if expected in types and (not isinstance(value,types[expected]) or expected == "integer" and isinstance(value,bool)): raise ValidationError(f"实例类型应为 {expected}")
    if isinstance(value,str):
        if len(value) < schema.get("minLength",0): raise ValidationError("字符串过短")
        if "pattern" in schema and not re.fullmatch(schema["pattern"],value): raise ValidationError("字符串格式无效")
    if isinstance(value,int) and value < schema.get("minimum",value): raise ValidationError("数值过小")
    if isinstance(value,(list,tuple)):
        if len(value) < schema.get("minItems",0): raise ValidationError("数组过短")
        if schema.get("uniqueItems") and len({repr(x) for x in value}) != len(value): raise ValidationError("数组元素重复")
        for item in value: validate_instance(item,schema.get("items",{}),root)
    if isinstance(value,dict):
        missing=set(schema.get("required",()))-set(value)
        if missing: raise ValidationError("实例缺少必填字段")
        props=schema.get("properties",{})
        if schema.get("additionalProperties") is False and set(value)-set(props): raise ValidationError("实例包含未知字段")
        for key,item in value.items():
            if key in props: validate_instance(item,props[key],root)
    return value
