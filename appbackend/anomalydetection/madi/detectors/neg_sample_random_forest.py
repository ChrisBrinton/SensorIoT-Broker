#     Copyright 2020 Google LLC
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         https://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.
"""Isolation Forest Anomaly Detector."""
from madi.detectors.base_detector import BaseAnomalyDetectionAlgorithm
import madi.utils.sample_utils as sample_utils
import numpy as np
import pandas as pd
import sklearn.ensemble

_CLASS_LABEL = 'class_label'
_NORMAL_CLASS = 1


class NegativeSamplingRandomForestAd(sklearn.ensemble.RandomForestClassifier,
                                     BaseAnomalyDetectionAlgorithm):
  """Anomaly Detector with a Random Forest Classifier and negative sampling."""

  def __init__(
      self,
      n_estimators=100,
      criterion='gini',
      max_depth=None,
      min_samples_split=2,
      min_samples_leaf=1,
      min_weight_fraction_leaf=0.0,
      max_features='sqrt',
      max_leaf_nodes=None,
      min_impurity_decrease=0.0,
      bootstrap=True,
      oob_score=False,
      n_jobs=None,
      random_state=None,
      verbose=0,
      warm_start=False,
      class_weight=None,
      ccp_alpha=0.0,
      max_samples=None,
      sample_ratio=2.0,
      sample_delta=0.05,
  ):
    """Constructs a NS-RF Anomaly Detector.

    Args:
      n_estimators: See sklearn.ensemble.RandomForestClassifier.
      criterion: See sklearn.ensemble.RandomForestClassifier.
      max_depth: See sklearn.ensemble.RandomForestClassifier.
      min_samples_split: See sklearn.ensemble.RandomForestClassifier.
      min_samples_leaf: See sklearn.ensemble.RandomForestClassifier.
      min_weight_fraction_leaf: See sklearn.ensemble.RandomForestClassifier.
      max_features: See sklearn.ensemble.RandomForestClassifier.
      max_leaf_nodes: See sklearn.ensemble.RandomForestClassifier.
      min_impurity_decrease: See sklearn.ensemble.RandomForestClassifier.
      bootstrap: See sklearn.ensemble.RandomForestClassifier.
      oob_score: See sklearn.ensemble.RandomForestClassifier.
      n_jobs: See sklearn.ensemble.RandomForestClassifier.
      random_state: See sklearn.ensemble.RandomForestClassifier.
      verbose: See sklearn.ensemble.RandomForestClassifier.
      warm_start: See sklearn.ensemble.RandomForestClassifier.
      class_weight: See sklearn.ensemble.RandomForestClassifier.
      ccp_alpha: See sklearn.ensemble.RandomForestClassifier.
      max_samples: See sklearn.ensemble.RandomForestClassifier.
      sample_ratio: ratio of negative sample size to positive sample size.
      sample_delta: sample extension beyond min and max limits of pos sample.
    """
    super(NegativeSamplingRandomForestAd, self).__init__(
        n_estimators=n_estimators,
        criterion=criterion,
        max_depth=max_depth,
        min_samples_split=min_samples_split,
        min_samples_leaf=min_samples_leaf,
        min_weight_fraction_leaf=min_weight_fraction_leaf,
        max_features=max_features,
        max_leaf_nodes=max_leaf_nodes,
        min_impurity_decrease=min_impurity_decrease,
        bootstrap=bootstrap,
        oob_score=oob_score,
        n_jobs=n_jobs,
        random_state=random_state,
        verbose=verbose,
        warm_start=warm_start,
        class_weight=class_weight,
        ccp_alpha=ccp_alpha,
        max_samples=max_samples,
    )
    self._normalization_info = None
    self.sample_ratio = sample_ratio
    self.sample_delta = sample_delta

  def train_model(self, x_train: pd.DataFrame) -> None:
    """Trains a NS-NN Anomaly detector using the positive sample.

    Args:
      x_train: training sample, which does not need to be normalized.
    """
    # TODO(sipple) Consolidate the normalization code into the base class.
    self._normalization_info = sample_utils.get_normalization_info(x_train)
    column_order = sample_utils.get_column_order(self._normalization_info)
    normalized_x_train = sample_utils.normalize(x_train[column_order],
                                                self._normalization_info)

    normalized_training_sample = sample_utils.apply_negative_sample(
        positive_sample=normalized_x_train,
        sample_ratio=self.sample_ratio,
        sample_delta=self.sample_delta)

    super(NegativeSamplingRandomForestAd, self).fit(
        X=normalized_training_sample[column_order],
        y=normalized_training_sample[_CLASS_LABEL])

  def predict(self, sample_df: pd.DataFrame) -> pd.DataFrame:
    """Performs anomaly detection on a new sample.

    Args:
      sample_df: dataframe with the new datapoints, not normalized.

    Returns:
      original dataframe with a new column labled 'class_prob' rangin from 1.0
      as normal to 0.0 as anomalous.
    """

    sample_df_normalized = sample_utils.normalize(sample_df,
                                                  self._normalization_info)
    column_order = sample_utils.get_column_order(self._normalization_info)
    x = sample_df_normalized[column_order].astype(np.float32)

    preds = super(NegativeSamplingRandomForestAd, self).predict_proba(x)
    sample_df['class_prob'] = preds[:, _NORMAL_CLASS]
    return sample_df
