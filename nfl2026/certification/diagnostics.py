"""Additional matched-population diagnostics; never refits or activates models."""
import pandas as pd
import numpy as np
from sklearn.metrics import average_precision_score


def supplemental_metrics(output):
    live=pd.read_csv(output/'live_starter_comparison_rows.csv')
    history=pd.read_csv(output/'historical_starter_rows.csv')
    opportunities=[]
    for label,d,role,prefix,actual in [('FROZEN_EXPECTED_STARTER',live,'starter_evidence','expected_',''),
            ('2025_DEPTH_STARTER_AVAILABILITY_UNPROVED',history,'expected_starter_depth','pred_','actual_')]:
        for area,b in d[d[role]].groupby('area'):
            target='carries' if area=='qb_rushing' else 'targets'
            error=b[prefix+target]-b[actual+target]
            opportunities.append(dict(population=label,area=area,rows=len(b),opportunity=target,
                incumbent_mae=error.abs().mean(),challenger_mae=error.abs().mean(),
                incumbent_bias=error.mean(),challenger_bias=error.mean(),
                opportunity_predictions_changed=False))
    pd.DataFrame(opportunities).to_csv(output/'opportunity_metrics.csv',index=False)
    intervals=pd.read_csv(output/'passing_forward_intervals.csv')
    starters=pd.read_csv(output/'passing_existing_model_starter_rows.csv')
    intervals=intervals.merge(starters[['game_id','team','player_id','expected_starter_depth']],
        on=['game_id','team','player_id'],how='left',validate='one_to_one')
    selected=intervals[intervals.expected_starter_depth.eq(True)]
    pd.DataFrame([dict(population='2025_DEPTH_QB1; intervals fitted on prior nominal QBs',rows=len(selected),
        coverage80=((selected.passing_yards>=selected.p10)&(selected.passing_yards<=selected.p90)).mean(),
        width80=(selected.p90-selected.p10).mean(),certified=False)]).to_csv(output/'passing_starter_interval_metrics.csv',index=False)
    thresholds=pd.read_csv(output/'threshold_rows.csv')
    reliability=[];outcomes=[]
    for area,b in thresholds[thresholds.starter_evidence].groupby('area'):
        # Each captured bookmaker threshold is retained; player-game clustered CIs
        # in threshold_metrics.csv account for correlated duplicate books/lines.
        b=b.dropna(subset=['actual_over','model_over_probability','challenger_over_probability'])
        for model,field in [('incumbent','model_over_probability'),('challenger_diagnostic','challenger_over_probability')]:
            p=b[field];bucket=np.minimum(9,(p*10).astype(int))
            for band,g in b.assign(probability=p,band=bucket).groupby('band'):
                reliability.append(dict(area=area,model=model,bin=int(band),lines=len(g),
                    mean_probability=g.probability.mean(),observed_over=g.actual_over.mean(),distribution_certified=False))
            outcomes.append(dict(area=area,model=model,book_lines=len(b),threshold_side_accuracy=(p.ge(.5)==b.actual_over.eq(1)).mean(),
                observed_over=b.actual_over.mean(),predicted_over=p.mean(),
                log_loss=float(-np.mean(b.actual_over*np.log(p.clip(1e-8,1-1e-8))+(1-b.actual_over)*np.log((1-p).clip(1e-8,1-1e-8)))),
                diagnostic_only=True,grading_equivalence_certified=False))
    pd.DataFrame(reliability).to_csv(output/'threshold_reliability.csv',index=False)
    pd.DataFrame(outcomes).to_csv(output/'threshold_outcomes.csv',index=False)
    coverage=[]
    for area,b in history.groupby('area'):
        teams=b[['game_id','team','depth_role_evidence']].drop_duplicates()
        coverage.append(dict(area=area,nominal_players=len(b),team_games=len(teams),
            role_snapshot_present=int(teams.depth_role_evidence.eq('TIMESTAMPED_PREGAME_DEPTH').sum()),
            stale_or_missing_team_games=int(teams.depth_role_evidence.eq('MISSING_OR_STALE').sum()),
            conflicted_team_games=int(teams.depth_role_evidence.eq('CONFLICT').sum()),
            depth_starter_rows=int(b.expected_starter_depth.sum()),active_eligibility_certified=False))
    pd.DataFrame(coverage).to_csv(output/'historical_role_coverage.csv',index=False)


def wr_confusion_metrics(output):
    """Conventional false-positive rates, separate from weak selected workload."""
    rows=pd.read_csv(output/'wr_ranked_validation_rows.csv');result=[]
    for (season,population),b in rows.groupby(['season','population']):
        high=b.actual_targets.ge(5)
        for selector,chosen in [('TOP_TWO',b.target_rank.le(2)),
                ('FEATURED_PLUS_RADAR',b.category.isin(['FEATURED','UNDER_THE_RADAR']))]:
            tp=int((chosen&high).sum());fp=int((chosen&~high).sum())
            fn=int((~chosen&high).sum());tn=int((~chosen&~high).sum())
            assert tp+fp+fn+tn==len(b)
            result.append(dict(season=int(season),population=population,selector=selector,
                true_positives=tp,false_positives=fp,false_negatives=fn,true_negatives=tn,
                recall=tp/(tp+fn),precision=tp/(tp+fp),false_positive_rate=fp/(fp+tn),
                weak_under2_selected=int((chosen&b.actual_targets.lt(2)).sum()),
                projected_target_average_precision=average_precision_score(high,b.expected_targets),
                projected_yardage_average_precision=average_precision_score(high,b.pred_receiving_yards),
                probability_or_projection_changed=False))
    pd.DataFrame(result).to_csv(output/'wr_confusion_metrics.csv',index=False)
