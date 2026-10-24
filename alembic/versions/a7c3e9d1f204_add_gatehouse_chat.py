"""add gatehouse chat (gate_sites, chat_threads, chat_messages)

Revision ID: a7c3e9d1f204
Revises: f82a937beba7
Create Date: 2026-09-30 12:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d1f204'
down_revision: Union[str, Sequence[str], None] = 'f82a937beba7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'gate_sites',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('app_id', sa.String(length=50), nullable=False),
        sa.Column('estate_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('code', sa.String(length=50), nullable=True),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['estate_id'], ['estates.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_gate_sites_app_id'), 'gate_sites', ['app_id'], unique=False)
    op.create_index(op.f('ix_gate_sites_estate_id'), 'gate_sites', ['estate_id'], unique=False)
    op.create_index('ix_gate_sites_app_id_is_active', 'gate_sites', ['app_id', 'is_active'], unique=False)

    op.create_table(
        'chat_threads',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('app_id', sa.String(length=50), nullable=False),
        sa.Column('resident_id', sa.Uuid(), nullable=False),
        sa.Column('gate_site_id', sa.Uuid(), nullable=True),
        sa.Column('unit_address', sa.String(length=150), nullable=True),
        sa.Column('resident_name', sa.String(length=255), nullable=True),
        sa.Column('subject', sa.String(length=100), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('has_active_emergency', sa.Boolean(), nullable=False),
        sa.Column('last_message_at', sa.DateTime(), nullable=False),
        sa.Column('last_message_text', sa.String(length=500), nullable=True),
        sa.Column('last_sender_role', sa.String(length=30), nullable=True),
        sa.Column('resident_unread_count', sa.Integer(), nullable=False),
        sa.Column('guard_unread_count', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['resident_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['gate_site_id'], ['gate_sites.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chat_threads_app_id'), 'chat_threads', ['app_id'], unique=False)
    op.create_index(op.f('ix_chat_threads_resident_id'), 'chat_threads', ['resident_id'], unique=False)
    op.create_index(op.f('ix_chat_threads_gate_site_id'), 'chat_threads', ['gate_site_id'], unique=False)
    op.create_index(op.f('ix_chat_threads_status'), 'chat_threads', ['status'], unique=False)
    op.create_index(op.f('ix_chat_threads_has_active_emergency'), 'chat_threads', ['has_active_emergency'], unique=False)
    op.create_index(op.f('ix_chat_threads_last_message_at'), 'chat_threads', ['last_message_at'], unique=False)
    op.create_index('ix_chat_threads_app_id_status', 'chat_threads', ['app_id', 'status'], unique=False)
    op.create_index('ix_chat_threads_resident_status', 'chat_threads', ['resident_id', 'status'], unique=False)
    op.create_index('ix_chat_threads_emergency_order', 'chat_threads', ['app_id', 'has_active_emergency', 'last_message_at'], unique=False)

    op.create_table(
        'chat_messages',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('thread_id', sa.Uuid(), nullable=False),
        sa.Column('sender_id', sa.Uuid(), nullable=False),
        sa.Column('sender_role', sa.String(length=30), nullable=False),
        sa.Column('sender_name', sa.String(length=255), nullable=False),
        sa.Column('officer_badge', sa.String(length=50), nullable=True),
        sa.Column('officer_post', sa.String(length=100), nullable=True),
        sa.Column('is_from_guard', sa.Boolean(), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('status_tag', sa.String(length=50), nullable=True),
        sa.Column('is_read', sa.Boolean(), nullable=False),
        sa.Column('read_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['thread_id'], ['chat_threads.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_chat_messages_thread_id'), 'chat_messages', ['thread_id'], unique=False)
    op.create_index(op.f('ix_chat_messages_sender_id'), 'chat_messages', ['sender_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_chat_messages_sender_id'), table_name='chat_messages')
    op.drop_index(op.f('ix_chat_messages_thread_id'), table_name='chat_messages')
    op.drop_table('chat_messages')

    op.drop_index('ix_chat_threads_emergency_order', table_name='chat_threads')
    op.drop_index('ix_chat_threads_resident_status', table_name='chat_threads')
    op.drop_index('ix_chat_threads_app_id_status', table_name='chat_threads')
    op.drop_index(op.f('ix_chat_threads_last_message_at'), table_name='chat_threads')
    op.drop_index(op.f('ix_chat_threads_has_active_emergency'), table_name='chat_threads')
    op.drop_index(op.f('ix_chat_threads_status'), table_name='chat_threads')
    op.drop_index(op.f('ix_chat_threads_gate_site_id'), table_name='chat_threads')
    op.drop_index(op.f('ix_chat_threads_resident_id'), table_name='chat_threads')
    op.drop_index(op.f('ix_chat_threads_app_id'), table_name='chat_threads')
    op.drop_table('chat_threads')

    op.drop_index('ix_gate_sites_app_id_is_active', table_name='gate_sites')
    op.drop_index(op.f('ix_gate_sites_estate_id'), table_name='gate_sites')
    op.drop_index(op.f('ix_gate_sites_app_id'), table_name='gate_sites')
    op.drop_table('gate_sites')
