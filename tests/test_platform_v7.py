"""Offline v7 contract and frozen-weights checks; run with unittest."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'services/ml'),str(ROOT/'libs/transit_core')]
from mlcore.platform_v7 import DEFAULT_MODELS, load_manifest, submission, predict_frame

class PlatformV7Tests(unittest.TestCase):
    def test_template_order_and_rounding(self):
        frame=pd.DataFrame({'sample_id':['b','a']})
        result=submission(frame,np.array([1.23456,-5.6789]),pd.DataFrame({'sample_id':['a','b']}))
        self.assertEqual(result.sample_id.tolist(),['a','b'])
        self.assertEqual(result.prediction.tolist(),[-5.679,1.235])

    def test_rejects_invalid_predictions_and_ids(self):
        frame=pd.DataFrame({'sample_id':['a','b']})
        for pred in [np.array([1,np.nan]),np.array([1,np.inf]),np.array([1])]:
            with self.assertRaises(ValueError):submission(frame,pred,frame)
        for ids in [['a','a'],['a','c']]:
            with self.assertRaises(ValueError):submission(frame,np.array([1,2]),pd.DataFrame({'sample_id':ids}))

    def test_frozen_checksums(self):
        meta=load_manifest(DEFAULT_MODELS)
        self.assertEqual(meta['weights'],{'residual_catboost':.8,'gru':.2})
        self.assertEqual(meta['n_train'],3893)

    def test_rejects_changed_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)
            (path/'weight').write_bytes(b'changed')
            (path/'manifest.json').write_text(json.dumps({'files_sha256':{'weight':'wrong'}}))
            with self.assertRaisesRegex(ValueError,'checksum'):load_manifest(path)

    def test_requires_official_hint(self):
        with self.assertRaisesRegex(ValueError,'cur_dev'):
            predict_frame(pd.DataFrame({'cur_dev':[np.nan]}))

if __name__=='__main__':unittest.main()
