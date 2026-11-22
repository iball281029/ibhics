"""Hero materials change with the theme and restore the exact dark palette."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_hero_material_theme_switch_is_reversible():
    if not shutil.which("node"):
        pytest.skip("Node.js is required for Three.js material checks")
    root = Path(__file__).parents[1] / "ibhics/manager/static"
    source = (root / "fabric3d.js").read_text()
    stage = "class Stage {" + source.split("class Stage {", 1)[1].split("\nfunction ", 1)[0]
    # Keep the Stage class only; browser renderers are never constructed.
    stage = stage[: stage.rfind("}") + 1]
    code = (
        r"""
const assert = require('node:assert/strict');
(async () => {
const THREE = await import(process.argv[1]);
const document = {documentElement:{dataset:{theme:'light'}}};
const reduced = {matches:true};
"""
        + stage
    )
    code += r"""
const m=new THREE.MeshBasicMaterial({color:0x3ef0ff,blending:THREE.AdditiveBlending});
const original=m.color.getHex(), blend=m.blending;
const objects=[{userData:{},material:m}];
const s=Object.create(Stage.prototype);
s.themeAware=true;s.materialThemes=new WeakMap();s.scene={traverse:fn=>objects.forEach(fn)};
s.tick(0,0);
assert.notEqual(m.color.getHex(),original);
assert.equal(m.blending,THREE.NormalBlending);
document.documentElement.dataset.theme='dark';s.tick(0,0);
assert.equal(m.color.getHex(),original);assert.equal(m.blending,blend);
document.documentElement.dataset.theme='light';s.tick(0,0);
const extra=new THREE.MeshBasicMaterial({color:0xff8fd8,blending:THREE.AdditiveBlending});
objects.push({userData:{},material:extra});s.tick(0,0);
assert.equal(extra.blending,THREE.NormalBlending);
document.documentElement.dataset.theme='dark';s.tick(0,0);
assert.equal(extra.color.getHex(),0xff8fd8);
assert.equal(extra.blending,THREE.AdditiveBlending);
s.themeAware=false;document.documentElement.dataset.theme='light';s.tick(0,0);
assert.equal(m.color.getHex(),original);assert.equal(m.blending,blend);
})().catch(e=>{console.error(e);process.exit(1);});
"""
    result = subprocess.run(
        ["node", "-e", code, (root / "vendor/three/three.module.min.js").as_uri()],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
