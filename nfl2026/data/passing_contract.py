"""Individual passing labels. Dropbacks/sacks never become pass attempts."""
import pandas as pd

VERSION='individual-passing-pbp-v1'


def aggregate_individual_passing(pbp):
    required={'game_id','play_id','posteam','passer_player_id','passing_yards','pass_touchdown',
        'pass_attempt','sack','two_point_attempt','play_type','complete_pass'}
    if not required.issubset(pbp) or pbp.duplicated(['game_id','play_id']).any():
        raise ValueError('Passing PBP schema/identity invalid')
    p=pbp[pbp.passer_player_id.notna()&pbp.posteam.notna()&pbp.play_type.ne('no_play')&pbp.two_point_attempt.ne(1)].copy()
    if p.loc[p.complete_pass.eq(1),'passing_yards'].isna().any():raise ValueError('Completed pass has unknown yardage')
    p['pass_attempts']=(p.pass_attempt.eq(1)&p.sack.ne(1)).astype(int)
    p['completions']=p.complete_pass.eq(1).astype(int)
    p['passing_tds']=p.pass_touchdown.fillna(0)
    p['passing_yards']=p.passing_yards.fillna(0)
    return p.groupby(['game_id','posteam','passer_player_id'])[['pass_attempts','completions','passing_yards','passing_tds']].sum().reset_index().rename(columns={'posteam':'team','passer_player_id':'player_id'})
